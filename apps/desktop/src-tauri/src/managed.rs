use std::{path::Path, process::Command};

pub fn run(source: &str, action: &str, model_key: Option<&str>) -> Result<String, String> {
    if ![
        "install",
        "start",
        "stop",
        "rebuild",
        "status",
        "logs",
        "diagnose",
        "credential",
        "configure-model",
        "uninstall",
    ]
    .contains(&action)
    {
        return Err("无效受管操作".into());
    }
    let root = Path::new(source)
        .canonicalize()
        .map_err(|_| "源码目录不存在")?;
    let script = root.join("deploy/compose/managed-kernel.ps1");
    if !script.is_file() || !root.join("services/kernel/Dockerfile").is_file() {
        return Err("请选择包含 deploy/compose/managed-kernel.ps1 的 niucai 源码目录".into());
    }
    let mut command = Command::new("powershell.exe");
    command
        .args([
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
        ])
        .arg(script)
        .arg("-Action")
        .arg(action);
    if let Some(key) = model_key {
        command.env("NIUCAI_MANAGED_MODEL_KEY", key);
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000);
    }
    let output = command.output().map_err(|_| "无法启动受管部署脚本")?;
    if !output.status.success() {
        let mut error = String::from_utf8_lossy(&output.stderr).to_string();
        if let Some(key) = model_key {
            error = error.replace(key, "[REDACTED]");
        }
        return Err(error.chars().take(2000).collect());
    }
    Ok(String::from_utf8_lossy(&output.stdout).to_string())
}

pub fn runner(source: &str, input: serde_json::Value) -> Result<String, String> {
    let root = Path::new(source)
        .canonicalize()
        .map_err(|_| "源码目录不存在")?;
    let script = root.join("deploy/compose/managed-runner.ps1");
    if !script.is_file() {
        return Err("受管 Runner 脚本不存在".into());
    }
    let id = input["id"].as_str().ok_or("缺少节点 ID")?;
    if id.len() != 36 || !id.chars().all(|c| c.is_ascii_hexdigit() || c == '-') {
        return Err("无效节点 ID".into());
    }
    let mut command = Command::new("powershell.exe");
    command
        .args([
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
        ])
        .arg(script)
        .args(["-NodeId", id, "-Action", "install"]);
    for (key, env) in [
        ("token", "NIUCAI_INSTALL_NODE_TOKEN"),
        ("modelKey", "NIUCAI_INSTALL_MODEL_KEY"),
        ("provider", "NIUCAI_INSTALL_PROVIDER"),
        ("model", "NIUCAI_INSTALL_MODEL"),
        ("baseUrl", "NIUCAI_INSTALL_BASE_URL"),
        ("kernelInstanceId", "NIUCAI_INSTALL_KERNEL_ID"),
    ] {
        command.env(env, input[key].as_str().unwrap_or(""));
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000);
    }
    let output = command.output().map_err(|_| "无法启动 Runner 安装脚本")?;
    if !output.status.success() {
        return Err("Runner 安装失败；检查 Docker、受管网络和源码目录".into());
    }
    Ok(String::from_utf8_lossy(&output.stdout).to_string())
}
