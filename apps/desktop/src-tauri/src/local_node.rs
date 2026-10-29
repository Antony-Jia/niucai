//! 本机 Windows Runner（sidecar）托管：启动、崩溃退避重启、经 stdin 优雅停止与状态读取。
//! Runner 以当前用户身份运行，凭据与工作区在 %LOCALAPPDATA%\niucai-runner，与 Runner 默认目录一致。
use serde::Serialize;
use std::{
    fs,
    io::{Read, Seek, SeekFrom, Write},
    path::PathBuf,
    process::{Child, ChildStdin, Command, Stdio},
    sync::{Arc, Mutex},
    thread,
    time::{Duration, Instant},
};

/// 这些退出原因重启也无济于事，停止托管并提示用户。
const FATAL: [&str; 4] = [
    "PAIRING_CODE_INVALID",
    "PAIRING_REQUIRED",
    "RUNNER_ALREADY_RUNNING",
    "KERNEL_IDENTITY_MISMATCH",
];
const LOG_LIMIT: u64 = 5 * 1024 * 1024;

#[derive(Default)]
struct Inner {
    child: Option<Child>,
    stdin: Option<ChildStdin>,
    wanted: bool,
    generation: u64,
    last_error: String,
}

#[derive(Default, Clone)]
pub struct LocalRunner(Arc<Mutex<Inner>>);

pub struct Launch {
    pub kernel_url: String,
    pub pairing_code: Option<String>,
    pub workdir_allow: Vec<String>,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Status {
    running: bool,
    wanted: bool,
    paired: bool,
    available: bool,
    data_dir: String,
    last_error: String,
    log: String,
}

pub fn data_dir() -> PathBuf {
    std::env::var_os("LOCALAPPDATA")
        .map(PathBuf::from)
        .unwrap_or_else(std::env::temp_dir)
        .join("niucai-runner")
}

fn credentials() -> PathBuf {
    data_dir().join("control").join("credentials.json")
}

/// 查找顺序：NIUCAI_RUNNER_EXE > 安装目录下的 sidecar > 开发期 binaries/。
fn executable() -> Option<PathBuf> {
    if let Some(path) = std::env::var_os("NIUCAI_RUNNER_EXE") {
        return Some(PathBuf::from(path)).filter(|p| p.is_file());
    }
    let beside = std::env::current_exe()
        .ok()
        .and_then(|exe| exe.parent().map(|dir| dir.join("niucai-runner.exe")));
    let dev = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("binaries")
        .join("niucai-runner-x86_64-pc-windows-msvc.exe");
    beside.into_iter().chain([dev]).find(|p| p.is_file())
}

fn log_tail(lines: usize) -> String {
    let Ok(mut file) = fs::File::open(data_dir().join("runner.log")) else {
        return String::new();
    };
    let size = file.metadata().map(|m| m.len()).unwrap_or(0);
    let _ = file.seek(SeekFrom::Start(size.saturating_sub(16 * 1024)));
    let mut bytes = Vec::new();
    let _ = file.read_to_end(&mut bytes);
    let text = String::from_utf8_lossy(&bytes);
    let all: Vec<&str> = text.lines().collect();
    all[all.len().saturating_sub(lines)..].join("\n")
}

fn spawn(launch: &Launch, first: bool) -> Result<Child, String> {
    let exe = executable()
        .ok_or("未找到 niucai-runner.exe，请先运行 services/node-runner/build-windows.ps1")?;
    let root = data_dir();
    fs::create_dir_all(&root).map_err(|_| "无法创建 Runner 数据目录")?;
    let log_path = root.join("runner.log");
    if fs::metadata(&log_path)
        .map(|m| m.len() > LOG_LIMIT)
        .unwrap_or(false)
    {
        let _ = fs::rename(&log_path, root.join("runner.log.1"));
    }
    let log = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&log_path)
        .map_err(|_| "无法写入 Runner 日志")?;
    let mut command = Command::new(exe);
    command
        .env("NIUCAI_KERNEL_URL", &launch.kernel_url)
        .env("NIUCAI_NODE_DATA", &root)
        .env("NIUCAI_RUNNER_STDIN_CONTROL", "1")
        .env("PYTHONUNBUFFERED", "1")
        .env_remove("NIUCAI_NODE_ID")
        .env_remove("NIUCAI_NODE_TOKEN")
        .stdin(Stdio::piped())
        .stdout(log.try_clone().map_err(|_| "无法写入 Runner 日志")?)
        .stderr(log);
    if !launch.workdir_allow.is_empty() {
        let joined =
            std::env::join_paths(&launch.workdir_allow).map_err(|_| "工作目录白名单无效")?;
        command.env("NIUCAI_WORKDIR_ALLOW", joined);
    }
    // 配对码只在首次启动时传入；之后 Runner 使用落盘的节点凭据。
    match (&launch.pairing_code, first) {
        (Some(code), true) => command.env("NIUCAI_PAIRING_CODE", code),
        _ => command.env_remove("NIUCAI_PAIRING_CODE"),
    };
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000);
    }
    command.spawn().map_err(|e| format!("无法启动 Runner：{e}"))
}

impl LocalRunner {
    pub fn start(&self, launch: Launch) -> Result<(), String> {
        let generation = {
            let mut inner = self.0.lock().unwrap();
            if inner.wanted || inner.child.is_some() {
                return Err("本机节点已在运行".into());
            }
            if launch.pairing_code.is_none() && !credentials().is_file() {
                return Err("本机尚未配对，请先生成配对码".into());
            }
            let mut child = spawn(&launch, true)?;
            inner.stdin = child.stdin.take();
            inner.child = Some(child);
            inner.wanted = true;
            inner.generation += 1;
            inner.last_error.clear();
            inner.generation
        };
        let runner = self.clone();
        thread::spawn(move || runner.supervise(launch, generation));
        Ok(())
    }

    /// 轮询子进程：意外退出时按 2s→60s 退避重启；致命原因或用户停止后不再重启。
    fn supervise(&self, launch: Launch, generation: u64) {
        let mut backoff = Duration::from_secs(2);
        loop {
            thread::sleep(Duration::from_millis(500));
            let mut inner = self.0.lock().unwrap();
            if inner.generation != generation {
                return;
            }
            let exited = match inner.child.as_mut().map(|c| c.try_wait()) {
                Some(Ok(Some(status))) => Some(status),
                Some(Ok(None)) => continue,
                _ => None,
            };
            inner.child = None;
            inner.stdin = None;
            if !inner.wanted {
                return;
            }
            let tail = log_tail(20);
            if let Some(reason) = FATAL.iter().find(|r| tail.contains(**r)) {
                inner.wanted = false;
                inner.last_error = format!("Runner 已停止：{reason}");
                return;
            }
            if exited.map(|s| s.success()).unwrap_or(false) && tail.contains("RUNNER_EXITED") {
                inner.wanted = false;
                return;
            }
            inner.last_error = format!(
                "Runner 意外退出（{}），{} 秒后重启",
                exited.map(|s| s.to_string()).unwrap_or_default(),
                backoff.as_secs()
            );
            drop(inner);
            let deadline = Instant::now() + backoff;
            while Instant::now() < deadline {
                thread::sleep(Duration::from_millis(250));
                let inner = self.0.lock().unwrap();
                if inner.generation != generation || !inner.wanted {
                    return;
                }
            }
            backoff = (backoff * 2).min(Duration::from_secs(60));
            let mut inner = self.0.lock().unwrap();
            if inner.generation != generation || !inner.wanted {
                return;
            }
            match spawn(&launch, false) {
                Ok(mut child) => {
                    inner.stdin = child.stdin.take();
                    inner.child = Some(child);
                }
                Err(error) => {
                    inner.wanted = false;
                    inner.last_error = error;
                    return;
                }
            }
        }
    }

    /// drain：不再领新作业，当前作业结束后退出；now：取消当前作业，20 秒后仍未退出则强杀。
    pub fn stop(&self, mode: &str) -> Result<(), String> {
        let word = match mode {
            "drain" => "drain",
            "now" => "stop",
            _ => return Err("无效停止方式".into()),
        };
        let mut inner = self.0.lock().unwrap();
        inner.wanted = false;
        if let Some(stdin) = inner.stdin.as_mut() {
            let _ = writeln!(stdin, "{word}").and_then(|_| stdin.flush());
        }
        if word == "stop" {
            let generation = inner.generation;
            let runner = self.clone();
            thread::spawn(move || {
                thread::sleep(Duration::from_secs(20));
                let mut inner = runner.0.lock().unwrap();
                if inner.generation == generation {
                    if let Some(child) = inner.child.as_mut() {
                        let _ = child.kill();
                    }
                }
            });
        }
        Ok(())
    }

    pub fn status(&self) -> Status {
        let mut inner = self.0.lock().unwrap();
        let running = match inner.child.as_mut().map(|c| c.try_wait()) {
            Some(Ok(None)) => true,
            _ => false,
        };
        Status {
            running,
            wanted: inner.wanted,
            paired: credentials().is_file(),
            available: executable().is_some(),
            data_dir: data_dir().to_string_lossy().to_string(),
            last_error: inner.last_error.clone(),
            log: log_tail(40),
        }
    }

    /// 解除本机配对：删除节点凭据与 Kernel 身份，下次启动需新配对码。仅在停止时允许。
    pub fn forget(&self) -> Result<(), String> {
        let inner = self.0.lock().unwrap();
        if inner.wanted || inner.child.is_some() {
            return Err("请先停止本机节点".into());
        }
        let root = data_dir();
        for path in [credentials(), root.join("kernel-identity.json")] {
            if path.exists() {
                fs::remove_file(&path).map_err(|_| "无法删除本机节点凭据")?;
            }
        }
        Ok(())
    }

    /// 桌面退出时调用：只发 drain，Runner 跑完当前作业后自行退出。
    pub fn drain_on_exit(&self) {
        let _ = self.stop("drain");
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stop_rejects_unknown_mode_and_forget_requires_stopped() {
        let runner = LocalRunner::default();
        assert!(runner.stop("kill").is_err());
        assert!(runner.stop("drain").is_ok());
        runner.0.lock().unwrap().wanted = true;
        assert!(runner.forget().is_err());
    }
}
