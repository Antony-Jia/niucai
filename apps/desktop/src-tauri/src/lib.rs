mod managed;
mod transport;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::{fs, sync::Mutex};
use tauri::{Manager, State, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_dialog::DialogExt;
use transport::{api_url, client, validate_url, Credentials};

#[derive(Clone, Default, Serialize, Deserialize)]
struct Profile {
    base_url: String,
    computer_url: String,
    remember_token: bool,
    #[serde(default)]
    kernel_instance_id: String,
}
#[derive(Serialize)]
struct ProfileView {
    #[serde(flatten)]
    profile: Profile,
    has_token: bool,
    platform: &'static str,
}
#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct ConnectionInput {
    base_url: String,
    computer_url: String,
    token: String,
    remember_token: bool,
}
#[derive(Default)]
struct KernelState {
    connection: Mutex<Option<Credentials>>,
    events: Mutex<Option<tokio::task::JoinHandle<()>>>,
    remote_computer: Mutex<Option<String>>,
}
fn profile_path(app: &tauri::AppHandle) -> Result<std::path::PathBuf, String> {
    let directory = app
        .path()
        .app_config_dir()
        .map_err(|_| "无法定位配置目录")?;
    fs::create_dir_all(&directory).map_err(|_| "无法创建配置目录")?;
    Ok(directory.join("connection.json"))
}
fn save_profile(app: &tauri::AppHandle, profile: &Profile) -> Result<(), String> {
    fs::write(
        profile_path(app)?,
        serde_json::to_vec_pretty(profile).map_err(|_| "配置格式错误")?,
    )
    .map_err(|_| "无法保存连接配置".into())
}
#[cfg(windows)]
fn credential_entry(base: &str) -> Result<keyring::Entry, String> {
    use sha2::{Digest, Sha256};
    let account = format!("{:x}", Sha256::digest(base.as_bytes()));
    keyring::Entry::new("niucai.desktop", &account)
        .map_err(|_| "无法打开 Windows 凭据管理器".into())
}
#[cfg(windows)]
fn remembered(base: &str) -> Option<String> {
    credential_entry(base).ok()?.get_password().ok()
}
#[cfg(not(windows))]
fn remembered(_base: &str) -> Option<String> {
    None
}
fn forget(base: &str) -> Result<(), String> {
    #[cfg(windows)]
    match credential_entry(base)?.delete_credential() {
        Ok(()) | Err(keyring::Error::NoEntry) => (),
        Err(_) => return Err("无法删除 Windows 凭据，请在凭据管理器中删除 niucai.desktop".into()),
    }
    #[cfg(not(windows))]
    let _ = base;
    Ok(())
}
fn trusted(window: &tauri::Webview) -> Result<(), String> {
    trusted_labels(window.label(), window.window().label())
}
fn trusted_labels(webview: &str, window: &str) -> Result<(), String> {
    if webview != "main" || window != "main" {
        return Err("此窗口没有 Kernel 权限".into());
    }
    Ok(())
}
fn stop(state: &KernelState) {
    if let Some(handle) = state.events.lock().unwrap().take() {
        handle.abort();
    }
}
fn connection(state: &KernelState) -> Result<Credentials, String> {
    state
        .connection
        .lock()
        .unwrap()
        .clone()
        .ok_or_else(|| "尚未连接 Kernel".into())
}
fn view(profile: Profile, has_token: bool) -> ProfileView {
    ProfileView {
        profile,
        has_token,
        platform: std::env::consts::OS,
    }
}
#[tauri::command]
async fn load_profile(
    window: tauri::Webview,
    app: tauri::AppHandle,
    state: State<'_, KernelState>,
) -> Result<ProfileView, String> {
    trusted(&window)?;
    let profile: Profile = fs::read(profile_path(&app)?)
        .ok()
        .and_then(|bytes| serde_json::from_slice(&bytes).ok())
        .unwrap_or_default();
    let token = if profile.remember_token {
        remembered(if profile.kernel_instance_id.is_empty() {
            &profile.base_url
        } else {
            &profile.kernel_instance_id
        })
    } else {
        None
    };
    if let Some(token) = token.as_ref() {
        if validate_url(&profile.base_url).is_ok() {
            let credentials = Credentials {
                base: profile.base_url.clone(),
                token: token.clone(),
                computer_url: profile.computer_url.clone(),
            };
            if !profile.kernel_instance_id.is_empty() {
                let identity =
                    transport::request(&credentials, "/api/kernel/identity", "GET", Value::Null)
                        .await;
                if let Ok(identity) = identity {
                    if identity["kernel_instance_id"].as_str() != Some(&profile.kernel_instance_id)
                    {
                        return Err("控制中心实例身份变化，请重新连接并核验".into());
                    }
                }
            }
            *state.connection.lock().unwrap() = Some(credentials);
        }
    }
    Ok(view(profile, token.is_some()))
}
#[tauri::command]
async fn connect_kernel(
    window: tauri::Webview,
    app: tauri::AppHandle,
    state: State<'_, KernelState>,
    input: ConnectionInput,
) -> Result<ProfileView, String> {
    trusted(&window)?;
    let base = validate_url(&input.base_url)?
        .to_string()
        .trim_end_matches('/')
        .to_string();
    if !input.computer_url.is_empty() {
        validate_url(&input.computer_url)?;
    }
    let current = state.connection.lock().unwrap().clone();
    let token = if input.token.is_empty() {
        current
            .as_ref()
            .filter(|c| c.base == base)
            .map(|c| c.token.clone())
            .or_else(|| {
                let saved: Option<Profile> = fs::read(profile_path(&app).ok()?)
                    .ok()
                    .and_then(|bytes| serde_json::from_slice(&bytes).ok());
                saved.filter(|p| p.base_url == base).and_then(|p| {
                    remembered(if p.kernel_instance_id.is_empty() {
                        &p.base_url
                    } else {
                        &p.kernel_instance_id
                    })
                })
            })
            .ok_or("请填写 API Token")?
    } else {
        input.token
    };
    if token.len() < 32 || token.contains('\r') || token.contains('\n') {
        return Err("API Token 至少需要 32 个字符".into());
    }
    let credentials = Credentials {
        base: base.clone(),
        token: token.clone(),
        computer_url: input.computer_url.clone(),
    };
    transport::request(&credentials, "/api/computers", "GET", Value::Null).await?;
    let identity =
        transport::request(&credentials, "/api/kernel/identity", "GET", Value::Null).await?;
    let instance_id = identity["kernel_instance_id"]
        .as_str()
        .ok_or("Kernel 未提供实例身份，请升级")?
        .to_string();
    if identity["protocol_version"].as_u64() != Some(2) {
        return Err("Kernel 协议版本不兼容".into());
    }
    #[cfg(windows)]
    if input.remember_token {
        credential_entry(&instance_id)?
            .set_password(&token)
            .map_err(|_| "无法保存到 Windows 凭据管理器")?;
    }
    #[cfg(not(windows))]
    if input.remember_token {
        return Err("此版本只在 Windows 支持保存凭据；请取消保存选项".into());
    }
    if !input.remember_token {
        forget(&instance_id)?;
    }
    let profile = Profile {
        base_url: base,
        computer_url: input.computer_url,
        remember_token: input.remember_token,
        kernel_instance_id: instance_id,
    };
    save_profile(&app, &profile)?;
    stop(&state);
    close_remote(&app)?;
    *state.connection.lock().unwrap() = Some(credentials);
    Ok(view(profile, true))
}
#[tauri::command]
fn disconnect_kernel(
    window: tauri::Webview,
    app: tauri::AppHandle,
    state: State<KernelState>,
) -> Result<(), String> {
    trusted(&window)?;
    stop(&state);
    close_remote(&app)?;
    if let Some(c) = state.connection.lock().unwrap().take() {
        let saved: Option<Profile> = fs::read(profile_path(&app)?)
            .ok()
            .and_then(|bytes| serde_json::from_slice(&bytes).ok());
        let instance_id = saved.map(|p| p.kernel_instance_id).unwrap_or_default();
        forget(if instance_id.is_empty() {
            &c.base
        } else {
            &instance_id
        })?;
        save_profile(
            &app,
            &Profile {
                base_url: c.base,
                computer_url: c.computer_url,
                remember_token: false,
                kernel_instance_id: instance_id,
            },
        )?;
    }
    Ok(())
}
#[tauri::command]
async fn api_request(
    window: tauri::Webview,
    state: State<'_, KernelState>,
    path: String,
    method: String,
    body: Value,
) -> Result<Value, String> {
    trusted(&window)?;
    transport::request(&connection(&state)?, &path, &method, body).await
}
#[tauri::command]
async fn open_computer(
    window: tauri::Webview,
    app: tauri::AppHandle,
    state: State<'_, KernelState>,
    computer_id: String,
) -> Result<(), String> {
    trusted(&window)?;
    let c = connection(&state)?;
    let computer = transport::request(
        &c,
        &format!("/api/computers/{computer_id}"),
        "GET",
        Value::Null,
    )
    .await?;
    if computer["control"] != "HUMAN" {
        return Err("请先取得 HUMAN 控制权".into());
    }
    if computer["kind"] != "linux" {
        return Err("此版本尚未接入 Android".into());
    }
    let remote = validate_url(&c.computer_url)?;
    if let Some(window) = app.get_webview_window("computer") {
        if state.remote_computer.lock().unwrap().as_deref() != Some(computer_id.as_str()) {
            return Err("请先关闭现有远程窗口，再切换电脑".into());
        }
        window.set_focus().map_err(|_| "无法聚焦桌面窗口")?;
        return Ok(());
    }
    // This async command avoids WebView2's synchronous-builder deadlock on Windows.
    // No remote-domain capabilities, initialization scripts, or Kernel token are provided.
    WebviewWindowBuilder::new(&app, "computer", WebviewUrl::External(remote))
        .title("niucai · Human Control")
        .inner_size(1280.0, 800.0)
        .min_inner_size(640.0, 480.0)
        .build()
        .map_err(|_| "无法创建远程桌面窗口")?;
    *state.remote_computer.lock().unwrap() = Some(computer_id.clone());
    // Check again after asynchronous WebView creation; ownership may have changed.
    match transport::request(
        &c,
        &format!("/api/computers/{computer_id}"),
        "GET",
        Value::Null,
    )
    .await
    {
        Ok(current) if current["control"] == "HUMAN" => Ok(()),
        _ => {
            close_remote(&app)?;
            Err("控制权已变化或无法确认，请重新接管".into())
        }
    }
}
#[derive(Deserialize)]
struct DesktopBounds {
    x: f64,
    y: f64,
    width: f64,
    height: f64,
    visible: bool,
}

#[tauri::command]
async fn embed_computer(
    window: tauri::Webview,
    app: tauri::AppHandle,
    state: State<'_, KernelState>,
    computer_id: String,
    bounds: DesktopBounds,
) -> Result<(), String> {
    trusted(&window)?;
    for coordinate in [bounds.x, bounds.y, bounds.width, bounds.height] {
        if !coordinate.is_finite() || !(0.0..=20000.0).contains(&coordinate) {
            return Err("无效桌面尺寸".into());
        }
    }
    if bounds.width < 50.0 || bounds.height < 50.0 {
        return Err("桌面区域过小".into());
    }
    if !bounds.visible {
        if let Some(view) = app.get_webview("computer-embedded") {
            view.hide().map_err(|_| "无法隐藏桌面")?;
        }
        return Ok(());
    }
    let c = connection(&state)?;
    let current = transport::request(
        &c,
        &format!("/api/computers/{computer_id}"),
        "GET",
        Value::Null,
    )
    .await?;
    if current["control"] != "HUMAN" || current["kind"] != "linux" {
        close_remote(&app)?;
        return Err("请先接管 Linux Computer".into());
    }
    let position = tauri::LogicalPosition::new(bounds.x, bounds.y);
    let size = tauri::LogicalSize::new(bounds.width, bounds.height);
    if let Some(view) = app.get_webview("computer-embedded") {
        if state.remote_computer.lock().unwrap().as_deref() != Some(computer_id.as_str()) {
            return Err("请先关闭桌面再切换电脑".into());
        }
        view.set_position(position).map_err(|_| "无法定位桌面")?;
        view.set_size(size).map_err(|_| "无法调整桌面尺寸")?;
        view.show().map_err(|_| "无法显示桌面")?;
    } else {
        close_remote(&app)?;
        let remote = validate_url(&c.computer_url)?;
        let parent = app.get_window("main").ok_or("主窗口不存在")?;
        // Separate WebView2 child: no iframe, remote IPC capability, token or script injection.
        // Async creation avoids the Windows synchronous-builder deadlock.
        parent
            .add_child(
                tauri::webview::WebviewBuilder::new(
                    "computer-embedded",
                    WebviewUrl::External(remote),
                ),
                position,
                size,
            )
            .map_err(|_| "无法创建内嵌桌面")?;
        *state.remote_computer.lock().unwrap() = Some(computer_id.clone());
    }
    match transport::request(
        &c,
        &format!("/api/computers/{computer_id}"),
        "GET",
        Value::Null,
    )
    .await
    {
        Ok(value) if value["control"] == "HUMAN" => Ok(()),
        _ => {
            close_remote(&app)?;
            Err("控制权已变化，请重新接管".into())
        }
    }
}

#[tauri::command]
fn close_computer(window: tauri::Webview, app: tauri::AppHandle) -> Result<(), String> {
    trusted(&window)?;
    close_remote(&app)
}
fn close_remote(app: &tauri::AppHandle) -> Result<(), String> {
    let mut failed = false;
    if let Some(view) = app.get_webview("computer-embedded") {
        let _ = view.hide();
        failed |= view.close().is_err();
    }
    if let Some(window) = app.get_webview_window("computer") {
        let _ = window.hide();
        failed |= window.close().is_err();
    }
    *app.state::<KernelState>().remote_computer.lock().unwrap() = None;
    if failed {
        Err("部分桌面窗口关闭失败，请关闭残留窗口".into())
    } else {
        Ok(())
    }
}
#[tauri::command]
fn toggle_computer_fullscreen(window: tauri::Webview, app: tauri::AppHandle) -> Result<(), String> {
    trusted(&window)?;
    let window = app
        .get_webview_window("computer")
        .ok_or("请先打开远程桌面")?;
    window
        .set_fullscreen(!window.is_fullscreen().map_err(|_| "无法读取窗口状态")?)
        .map_err(|_| "无法切换全屏".into())
}
#[tauri::command]
fn stop_events(window: tauri::Webview, state: State<KernelState>) -> Result<(), String> {
    trusted(&window)?;
    stop(&state);
    Ok(())
}
#[tauri::command]
async fn start_events(
    window: tauri::Webview,
    app: tauri::AppHandle,
    state: State<'_, KernelState>,
    after: u64,
) -> Result<(), String> {
    trusted(&window)?;
    let c = connection(&state)?;
    stop(&state);
    let handle = tokio::spawn(async move {
        transport::stream(app, c, after).await;
    });
    *state.events.lock().unwrap() = Some(handle);
    Ok(())
}
#[tauri::command]
async fn download_artifact(
    window: tauri::Webview,
    app: tauri::AppHandle,
    state: State<'_, KernelState>,
    artifact_id: String,
) -> Result<Option<String>, String> {
    trusted(&window)?;
    let c = connection(&state)?;
    let artifact = transport::request(
        &c,
        &format!("/api/artifacts/{artifact_id}"),
        "GET",
        Value::Null,
    )
    .await?;
    let filename: String = artifact["path"]
        .as_str()
        .unwrap_or("artifact")
        .rsplit(['/', '\\'])
        .next()
        .unwrap_or("artifact")
        .chars()
        .take(150)
        .map(|ch| {
            if "<>:\"/\\|?*".contains(ch) || ch.is_control() {
                '_'
            } else {
                ch
            }
        })
        .collect();
    let target = tauri::async_runtime::spawn_blocking(move || {
        app.dialog()
            .file()
            .set_file_name(filename)
            .blocking_save_file()
            .and_then(|file| file.into_path().ok())
    })
    .await
    .map_err(|_| "无法打开保存对话框")?;
    let Some(target) = target else {
        return Ok(None);
    };
    let mut response = client()?
        .get(api_url(
            &c.base,
            &format!("/api/artifacts/{artifact_id}/content"),
        )?)
        .bearer_auth(&c.token)
        .send()
        .await
        .map_err(|_| "无法下载文件")?;
    if !response.status().is_success() {
        return Err(format!("文件下载失败（{}）", response.status().as_u16()));
    }
    let mut bytes = Vec::new();
    while let Some(chunk) = response.chunk().await.map_err(|_| "文件传输中断")? {
        if bytes.len() + chunk.len() > 100 * 1024 * 1024 {
            return Err("此版本仅支持下载 100 MB 以内的文件".into());
        }
        bytes.extend_from_slice(&chunk);
    }
    fs::write(&target, bytes).map_err(|_| "无法保存文件")?;
    Ok(Some(target.to_string_lossy().to_string()))
}
#[tauri::command]
async fn save_resource_package(
    window: tauri::Webview,
    app: tauri::AppHandle,
    node_id: String,
    bytes: Vec<u8>,
) -> Result<Option<String>, String> {
    trusted(&window)?;
    if node_id.len() != 36
        || !node_id
            .chars()
            .all(|ch| ch.is_ascii_hexdigit() || ch == '-')
        || bytes.len() > 1024 * 1024
        || !bytes.starts_with(b"PK\x03\x04")
    {
        return Err("无效资源配置包".into());
    }
    let target = tauri::async_runtime::spawn_blocking(move || {
        app.dialog()
            .file()
            .set_file_name(format!("niucai-node-{node_id}.zip"))
            .blocking_save_file()
            .and_then(|file| file.into_path().ok())
    })
    .await
    .map_err(|_| "无法打开保存对话框")?;
    let Some(target) = target else {
        return Ok(None);
    };
    fs::write(&target, bytes).map_err(|_| "无法保存配置包")?;
    Ok(Some(target.to_string_lossy().to_string()))
}

#[tauri::command]
async fn managed_kernel(
    window: tauri::Webview,
    app: tauri::AppHandle,
    state: State<'_, KernelState>,
    source: String,
    action: String,
    model_key: Option<String>,
) -> Result<String, String> {
    trusted(&window)?;
    if action == "credential" {
        return Err("凭据不通过前端返回".into());
    }
    let action_for_script = action.clone();
    let source_for_script = source.clone();
    let result = tauri::async_runtime::spawn_blocking(move || {
        managed::run(&source_for_script, &action_for_script, model_key.as_deref())
    })
    .await
    .map_err(|_| "受管操作被中断")??;
    if ["install", "start", "rebuild"].contains(&action.as_str()) {
        let token =
            tauri::async_runtime::spawn_blocking(move || managed::run(&source, "credential", None))
                .await
                .map_err(|_| "无法读取本机凭据")??
                .trim()
                .to_string();
        connect_kernel(
            window,
            app,
            state,
            ConnectionInput {
                base_url: "http://127.0.0.1:8080".into(),
                computer_url: String::new(),
                token,
                remember_token: true,
            },
        )
        .await?;
    }
    Ok(result)
}

#[tauri::command]
async fn install_managed_runner(
    window: tauri::Webview,
    state: State<'_, KernelState>,
    source: String,
    input: Value,
) -> Result<String, String> {
    trusted(&window)?;
    let credentials = connection(&state)?;
    if credentials.base != "http://127.0.0.1:8080" {
        return Err("自动安装仅用于本机受管 Kernel；其他拓扑使用配置包".into());
    }
    let identity =
        transport::request(&credentials, "/api/kernel/identity", "GET", Value::Null).await?;
    if identity["kernel_instance_id"] != input["kernelInstanceId"] {
        return Err("Kernel 实例身份变化，请重新配置节点".into());
    }
    tauri::async_runtime::spawn_blocking(move || managed::runner(&source, input))
        .await
        .map_err(|_| "Runner 安装被中断")?
}

pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .manage(KernelState::default())
        .invoke_handler(tauri::generate_handler![
            load_profile,
            connect_kernel,
            disconnect_kernel,
            api_request,
            open_computer,
            embed_computer,
            close_computer,
            toggle_computer_fullscreen,
            start_events,
            stop_events,
            download_artifact,
            save_resource_package,
            managed_kernel,
            install_managed_runner
        ])
        .run(tauri::generate_context!())
        .expect("niucai desktop failed to start");
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn only_local_main_webview_has_kernel_access() {
        assert!(trusted_labels("main", "main").is_ok());
        assert!(trusted_labels("computer-embedded", "main").is_err());
        assert!(trusted_labels("computer", "computer").is_err());
        assert!(trusted_labels("main", "computer").is_err());
    }
    #[test]
    fn profile_contains_no_token() {
        let serialized = serde_json::to_string(&Profile {
            base_url: "https://example.com".into(),
            computer_url: String::new(),
            remember_token: true,
            kernel_instance_id: "test-instance".into(),
        })
        .unwrap();
        assert!(!serialized.contains("secret-token"));
        assert!(!serialized.contains("\"token\""));
    }
}
