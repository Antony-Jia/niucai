mod local_node;
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
    #[serde(default)]
    name: String,
}
#[derive(Default, Serialize, Deserialize)]
struct Store {
    #[serde(default)]
    active: String,
    #[serde(default)]
    profiles: Vec<Profile>,
}
#[derive(Serialize)]
struct ConnectionList {
    active: String,
    connections: Vec<ProfileView>,
}
#[derive(Serialize)]
struct Probe {
    key: String,
    status: &'static str,
    error: Option<String>,
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
    #[serde(default)]
    name: Option<String>,
}
#[derive(Default)]
struct KernelState {
    connection: Mutex<Option<Credentials>>,
    events: Mutex<Option<tokio::task::JoinHandle<()>>>,
    remote_computer: Mutex<Option<String>>,
}
fn profile_key(profile: &Profile) -> &str {
    if profile.kernel_instance_id.is_empty() {
        &profile.base_url
    } else {
        &profile.kernel_instance_id
    }
}
fn default_name(base: &str) -> String {
    match url::Url::parse(base)
        .ok()
        .and_then(|u| u.host_str().map(str::to_string))
    {
        Some(host) if matches!(host.as_str(), "localhost" | "127.0.0.1" | "[::1]" | "::1") => {
            "本机".into()
        }
        Some(host) => host,
        None => base.to_string(),
    }
}
// 同一实例重新配置地址时以最后一次保存为准，避免旧端口抢占当前连接。
fn normalize_store(mut store: Store) -> Store {
    let mut seen = std::collections::HashSet::new();
    store.profiles.reverse();
    store
        .profiles
        .retain(|p| seen.insert(profile_key(p).to_string()));
    store.profiles.reverse();
    store
}
// connections.json 保存多条连接（不含 Token）；旧版 connection.json 在首次读取时迁入。
fn read_store(app: &tauri::AppHandle) -> Store {
    let Ok(directory) = app.path().app_config_dir() else {
        return Store::default();
    };
    if let Some(store) = fs::read(directory.join("connections.json"))
        .ok()
        .and_then(|bytes| serde_json::from_slice::<Store>(&bytes).ok())
    {
        return normalize_store(store);
    }
    fs::read(directory.join("connection.json"))
        .ok()
        .and_then(|bytes| serde_json::from_slice::<Profile>(&bytes).ok())
        .filter(|p| !p.base_url.is_empty())
        .map(|mut p| {
            if p.name.is_empty() {
                p.name = default_name(&p.base_url);
            }
            Store {
                active: profile_key(&p).to_string(),
                profiles: vec![p],
            }
        })
        .unwrap_or_default()
}
fn write_store(app: &tauri::AppHandle, store: &Store) -> Result<(), String> {
    let directory = app
        .path()
        .app_config_dir()
        .map_err(|_| "无法定位配置目录")?;
    fs::create_dir_all(&directory).map_err(|_| "无法创建配置目录")?;
    fs::write(
        directory.join("connections.json"),
        serde_json::to_vec_pretty(store).map_err(|_| "配置格式错误")?,
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
    let store = read_store(&app);
    let profile: Profile = store
        .profiles
        .iter()
        .find(|p| profile_key(p) == store.active)
        .cloned()
        .unwrap_or_default();
    let token = if profile.remember_token {
        remembered(profile_key(&profile))
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
                read_store(&app)
                    .profiles
                    .iter()
                    .find(|p| p.base_url == base)
                    .and_then(|p| remembered(profile_key(p)))
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
    let mut store = read_store(&app);
    let existing = store
        .profiles
        .iter()
        .position(|p| profile_key(p) == instance_id || p.base_url == base);
    let name = input
        .name
        .map(|n| n.trim().chars().take(40).collect::<String>())
        .filter(|n| !n.is_empty())
        .or_else(|| {
            existing
                .map(|i| store.profiles[i].name.clone())
                .filter(|n| !n.is_empty())
        })
        .unwrap_or_else(|| default_name(&base));
    let profile = Profile {
        base_url: base,
        computer_url: input.computer_url,
        remember_token: input.remember_token,
        kernel_instance_id: instance_id.clone(),
        name,
    };
    match existing {
        Some(i) => {
            let old = profile_key(&store.profiles[i]).to_string();
            if old != instance_id {
                forget(&old)?;
            }
            store.profiles[i] = profile.clone();
        }
        None => store.profiles.push(profile.clone()),
    }
    store.active = instance_id;
    write_store(&app, &store)?;
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
        // 只断开并清除当前连接的凭据；其他已保存连接不受影响。
        let mut store = read_store(&app);
        let position = store
            .profiles
            .iter()
            .position(|p| profile_key(p) == store.active)
            .or_else(|| store.profiles.iter().position(|p| p.base_url == c.base));
        match position {
            Some(i) => {
                forget(profile_key(&store.profiles[i]))?;
                store.profiles[i].remember_token = false;
            }
            None => forget(&c.base)?,
        }
        store.active.clear();
        write_store(&app, &store)?;
    }
    Ok(())
}
#[tauri::command]
fn list_connections(
    window: tauri::Webview,
    app: tauri::AppHandle,
) -> Result<ConnectionList, String> {
    trusted(&window)?;
    let store = read_store(&app);
    let connections = store
        .profiles
        .iter()
        .map(|p| view(p.clone(), remembered(profile_key(p)).is_some()))
        .collect();
    Ok(ConnectionList {
        active: store.active,
        connections,
    })
}
#[tauri::command]
async fn activate_connection(
    window: tauri::Webview,
    app: tauri::AppHandle,
    state: State<'_, KernelState>,
    key: String,
) -> Result<ProfileView, String> {
    trusted(&window)?;
    let mut store = read_store(&app);
    let profile = store
        .profiles
        .iter()
        .find(|p| profile_key(p) == key)
        .cloned()
        .ok_or("未找到该连接")?;
    validate_url(&profile.base_url)?;
    let token = remembered(profile_key(&profile))
        .ok_or("该连接没有保存 Token，请在连接表单中重新输入并验证")?;
    let credentials = Credentials {
        base: profile.base_url.clone(),
        token,
        computer_url: profile.computer_url.clone(),
    };
    if !profile.kernel_instance_id.is_empty() {
        // 服务器暂时不可达时仍允许切换，由前端的重连流程处理；身份变化则必须拒绝。
        if let Ok(identity) =
            transport::request(&credentials, "/api/kernel/identity", "GET", Value::Null).await
        {
            if identity["kernel_instance_id"].as_str() != Some(&profile.kernel_instance_id) {
                return Err("控制中心实例身份变化，请重新连接并核验".into());
            }
        }
    }
    stop(&state);
    close_remote(&app)?;
    *state.connection.lock().unwrap() = Some(credentials);
    store.active = key;
    write_store(&app, &store)?;
    Ok(view(profile, true))
}
#[tauri::command]
fn remove_connection(
    window: tauri::Webview,
    app: tauri::AppHandle,
    state: State<KernelState>,
    key: String,
) -> Result<(), String> {
    trusted(&window)?;
    let mut store = read_store(&app);
    let Some(i) = store.profiles.iter().position(|p| profile_key(p) == key) else {
        return Ok(());
    };
    forget(&key)?;
    store.profiles.remove(i);
    if store.active == key {
        stop(&state);
        close_remote(&app)?;
        state.connection.lock().unwrap().take();
        store.active.clear();
    }
    write_store(&app, &store)
}
#[tauri::command]
async fn probe_connections(
    window: tauri::Webview,
    app: tauri::AppHandle,
    state: State<'_, KernelState>,
) -> Result<Vec<Probe>, String> {
    trusted(&window)?;
    let store = read_store(&app);
    let active = state.connection.lock().unwrap().clone();
    let probes = store.profiles.into_iter().map(|p| {
        let key = profile_key(&p).to_string();
        let token = match active.as_ref().filter(|c| c.base == p.base_url) {
            Some(c) => Some(c.token.clone()),
            None => remembered(&key),
        };
        async move {
            let Some(token) = token else {
                return Probe {
                    key,
                    status: "no_token",
                    error: None,
                };
            };
            let credentials = Credentials {
                base: p.base_url,
                token,
                computer_url: p.computer_url,
            };
            let probe = tokio::time::timeout(
                std::time::Duration::from_secs(6),
                transport::request(&credentials, "/api/computers", "GET", Value::Null),
            )
            .await;
            match probe {
                Ok(Ok(_)) => Probe {
                    key,
                    status: "online",
                    error: None,
                },
                Ok(Err(error)) if error.contains("API Token") => Probe {
                    key,
                    status: "auth",
                    error: Some(error),
                },
                Ok(Err(error)) => Probe {
                    key,
                    status: "offline",
                    error: Some(error),
                },
                Err(_) => Probe {
                    key,
                    status: "offline",
                    error: Some("连接超时".into()),
                },
            }
        }
    });
    Ok(futures_util::future::join_all(probes).await)
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
/// 节点（VPS/Docker 里的 Chrome + KasmVNC）登记的画面预览地址。
/// 只信 Kernel 拓扑里记录的地址，前端不能传入任意 URL。
fn node_preview_url(topology: &Value, node_id: &str) -> Result<url::Url, String> {
    let preview = topology["nodes"]
        .as_array()
        .and_then(|nodes| nodes.iter().find(|node| node["id"] == node_id))
        .and_then(|node| node["preview_url"].as_str())
        .filter(|value| !value.trim().is_empty())
        .ok_or("该节点没有登记画面预览地址")?;
    validate_url(preview)
}
fn preview_label(node_id: &str) -> String {
    let safe: String = node_id
        .chars()
        .filter(|c| c.is_ascii_alphanumeric() || *c == '-')
        .take(48)
        .collect();
    format!("node-preview-{safe}")
}
/// 在独立窗口里打开节点的远程浏览器画面（KasmVNC）。
/// 窗口没有 Kernel 权限、令牌或注入脚本；VNC 密码由用户在画面页里输入，不经过 Kernel 或 Pi。
#[tauri::command]
async fn open_node_preview(
    window: tauri::Webview,
    app: tauri::AppHandle,
    state: State<'_, KernelState>,
    node_id: String,
) -> Result<(), String> {
    trusted(&window)?;
    let c = connection(&state)?;
    let topology = transport::request(&c, "/api/topology", "GET", Value::Null).await?;
    let remote = node_preview_url(&topology, &node_id)?;
    let label = preview_label(&node_id);
    if let Some(existing) = app.get_webview_window(&label) {
        existing.set_focus().map_err(|_| "无法聚焦画面窗口")?;
        return Ok(());
    }
    WebviewWindowBuilder::new(&app, label, WebviewUrl::External(remote))
        .title("niucai · 节点画面")
        .inner_size(1280.0, 800.0)
        .min_inner_size(640.0, 480.0)
        .build()
        .map_err(|_| "无法创建画面窗口")?;
    Ok(())
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
                name: Some("本机".into()),
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

#[tauri::command]
async fn local_runner_start(
    window: tauri::Webview,
    state: State<'_, KernelState>,
    runner: State<'_, local_node::LocalRunner>,
    pairing_code: Option<String>,
    workdir_allow: Option<Vec<String>>,
) -> Result<(), String> {
    trusted(&window)?;
    let credentials = connection(&state)?;
    let pairing_code = pairing_code
        .map(|code| code.trim().to_string())
        .filter(|code| !code.is_empty());
    if let Some(code) = &pairing_code {
        if !code.chars().all(|c| c.is_ascii_alphanumeric() || c == '-') || code.len() > 16 {
            return Err("配对码格式无效".into());
        }
    }
    runner.start(local_node::Launch {
        kernel_url: credentials.base,
        pairing_code,
        workdir_allow: workdir_allow.unwrap_or_default(),
    })
}

#[tauri::command]
fn local_runner_stop(
    window: tauri::Webview,
    runner: State<'_, local_node::LocalRunner>,
    mode: String,
) -> Result<(), String> {
    trusted(&window)?;
    runner.stop(&mode)
}

#[tauri::command]
fn local_runner_status(
    window: tauri::Webview,
    runner: State<'_, local_node::LocalRunner>,
) -> Result<local_node::Status, String> {
    trusted(&window)?;
    Ok(runner.status())
}

#[tauri::command]
fn local_runner_forget(
    window: tauri::Webview,
    runner: State<'_, local_node::LocalRunner>,
) -> Result<(), String> {
    trusted(&window)?;
    runner.forget()
}

pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .manage(KernelState::default())
        .manage(local_node::LocalRunner::default())
        .invoke_handler(tauri::generate_handler![
            load_profile,
            connect_kernel,
            disconnect_kernel,
            list_connections,
            activate_connection,
            remove_connection,
            probe_connections,
            api_request,
            open_computer,
            embed_computer,
            close_computer,
            open_node_preview,
            toggle_computer_fullscreen,
            start_events,
            stop_events,
            download_artifact,
            save_resource_package,
            managed_kernel,
            install_managed_runner,
            local_runner_start,
            local_runner_stop,
            local_runner_status,
            local_runner_forget
        ])
        .build(tauri::generate_context!())
        .expect("niucai desktop failed to start")
        .run(|app, event| {
            if let tauri::RunEvent::Exit = event {
                app.state::<local_node::LocalRunner>().drain_on_exit();
            }
        });
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn latest_address_wins_for_same_kernel() {
        let store: Store = serde_json::from_str(r#"{"active":"k","profiles":[{"base_url":"http://127.0.0.1:18080","computer_url":"","remember_token":true,"kernel_instance_id":"k"},{"base_url":"http://127.0.0.1:8080","computer_url":"","remember_token":true,"kernel_instance_id":"k"}]}"#).unwrap();
        let normalized = normalize_store(store);
        assert_eq!(normalized.profiles.len(), 1);
        assert_eq!(normalized.profiles[0].base_url, "http://127.0.0.1:8080");
        assert_eq!(normalized.active, "k");
    }
    #[test]
    fn only_local_main_webview_has_kernel_access() {
        assert!(trusted_labels("main", "main").is_ok());
        assert!(trusted_labels("computer-embedded", "main").is_err());
        assert!(trusted_labels("computer", "computer").is_err());
        assert!(trusted_labels("main", "computer").is_err());
    }
    #[test]
    fn node_preview_only_uses_the_registered_safe_address() {
        let topology = serde_json::json!({"nodes": [
            {"id": "n1", "preview_url": "https://vnc.example.com:6901/"},
            {"id": "n2", "preview_url": "http://127.0.0.1:6901/"},
            {"id": "n3", "preview_url": "http://vnc.example.com/"},
            {"id": "n4", "preview_url": ""},
        ]});
        assert!(node_preview_url(&topology, "n1").is_ok());
        assert!(node_preview_url(&topology, "n2").is_ok());
        assert!(node_preview_url(&topology, "n3").is_err());
        assert!(node_preview_url(&topology, "n4").is_err());
        assert!(node_preview_url(&topology, "missing").is_err());
        assert_eq!(preview_label("a/b c-1"), "node-preview-abc-1");
    }
    #[test]
    fn connection_names_and_keys() {
        assert_eq!(default_name("http://127.0.0.1:8080"), "本机");
        assert_eq!(
            default_name("https://kernel.example.com"),
            "kernel.example.com"
        );
        let mut profile = Profile {
            base_url: "https://a.example".into(),
            ..Profile::default()
        };
        assert_eq!(profile_key(&profile), "https://a.example");
        profile.kernel_instance_id = "id-1".into();
        assert_eq!(profile_key(&profile), "id-1");
    }
    #[test]
    fn legacy_store_defaults_are_empty() {
        let store: Store = serde_json::from_str("{}").unwrap();
        assert!(store.active.is_empty() && store.profiles.is_empty());
        let old: Profile = serde_json::from_str(
            r#"{"base_url":"http://x","computer_url":"","remember_token":true}"#,
        )
        .unwrap();
        assert!(old.name.is_empty() && old.kernel_instance_id.is_empty());
    }
    #[test]
    fn profile_contains_no_token() {
        let serialized = serde_json::to_string(&Profile {
            base_url: "https://example.com".into(),
            computer_url: String::new(),
            remember_token: true,
            kernel_instance_id: "test-instance".into(),
            name: "测试".into(),
        })
        .unwrap();
        assert!(!serialized.contains("secret-token"));
        assert!(!serialized.contains("\"token\""));
    }
}
