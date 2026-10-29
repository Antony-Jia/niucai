"""Node service (Linux / Windows). Owns isolated engine CLI process trees, not model reasoning.

Linux：每个引擎独立 UID + 进程组；Windows：当前用户身份 + Job Object + 工作目录白名单（P4）。
"""

import asyncio
import base64
import contextlib
import hmac
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from hashlib import sha256
from pathlib import Path

import httpx

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

WINDOWS = os.name == "nt"
RUNNER_VERSION = "p4"
SAFE_ID = re.compile(r"[a-zA-Z0-9-]{1,36}")
CARD_WORKSPACE = re.compile(r"card-[a-zA-Z0-9-]{1,36}")
MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}")
BOARD_TOOL = re.compile(r"[a-z_]{1,40}")
BOARD_SCRIPT = Path(__file__).with_name("board_mcp.py")
ASSET_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,99}")
HEADER_KEY = re.compile(r"[A-Za-z0-9-]{1,100}")
# Windows 下经 .cmd 垫片启动 CLI 时参数会经过 cmd.exe，含这些字符的参数直接拒绝。
CMD_UNSAFE = re.compile(r"[&|<>^%!\r\n]")
CLAIM_WAIT = 15  # 长轮询秒数，需小于 Kernel 判定节点离线的租约（默认 30 秒）


def platform():
    return "windows" if WINDOWS else "linux"


def engine_uid(variable, default):
    """Linux 每个引擎一个 UID；Windows 没有 setuid，以当前用户身份运行（None）。"""
    return None if WINDOWS else int(os.environ.get(variable, default))


def chown(path, uid):
    if uid is not None:
        os.chown(path, uid, uid)


def default_data_dir():
    if WINDOWS:
        return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "niucai-runner"
    return Path("/data")


WINDOWS_ENVIRONMENT = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "USERNAME",
    "HOMEDRIVE",
    "HOMEPATH",
    "APPDATA",
    "LOCALAPPDATA",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
    "OS",
)


def base_environment(home):
    """引擎进程的最小环境；Windows 保留系统目录与用户目录，CLI 才能找到用户已登录的账号。"""
    if not WINDOWS:
        return {"PATH": os.environ["PATH"], "HOME": str(home), "LANG": "C.UTF-8"}
    environment = {name: os.environ[name] for name in WINDOWS_ENVIRONMENT if name in os.environ}
    environment["PYTHONUTF8"] = "1"
    return environment


def resolve_command(args):
    """Windows：解析 npm 垫片（claude.cmd / codex.cmd）的完整路径，并拒绝 cmd.exe 会解释的参数。"""
    if not WINDOWS:
        return args
    executable = shutil.which(args[0])
    if not executable:
        raise RuntimeError(f"{args[0]} not found on PATH")
    if executable.lower().endswith((".cmd", ".bat")) and any(CMD_UNSAFE.search(str(a)) for a in args[1:]):
        raise RuntimeError("argument contains characters unsafe for a .cmd launcher")
    return [executable, *args[1:]]


def workdir_allowlist():
    return [Path(p) for p in os.environ.get("NIUCAI_WORKDIR_ALLOW", "").split(os.pathsep) if p.strip()]


def allowed_workdir(raw):
    """卡片指定的本机目录：必须位于节点白名单（NIUCAI_WORKDIR_ALLOW，按路径分隔符分隔）内且已存在。"""
    candidate = Path(str(raw))
    if not candidate.is_absolute():
        raise RuntimeError("workdir must be an absolute path")
    resolved = candidate.resolve()
    roots = [root.resolve() for root in workdir_allowlist()]
    if not any(resolved == root or resolved.is_relative_to(root) for root in roots):
        raise RuntimeError("workdir is not in this node's allowlist (NIUCAI_WORKDIR_ALLOW)")
    if not resolved.is_dir():
        raise RuntimeError("workdir does not exist on this node")
    return resolved


def atomic_json(path, value, owner=None):
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=".runner-")
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
            if owner is not None:
                os.fchown(stream.fileno(), owner, owner)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def node_credentials(control, kernel_url):
    """节点凭据来源：环境变量 > control/credentials.json > 用 NIUCAI_PAIRING_CODE 一次性兑换。"""
    if os.environ.get("NIUCAI_NODE_ID") and os.environ.get("NIUCAI_NODE_TOKEN"):
        return os.environ["NIUCAI_NODE_ID"], os.environ["NIUCAI_NODE_TOKEN"], None
    stored = control / "credentials.json"
    if stored.exists():
        value = json.loads(stored.read_text())
        return value["node_id"], value["token"], None
    code = os.environ.get("NIUCAI_PAIRING_CODE", "").strip()
    if not code:
        raise SystemExit("PAIRING_REQUIRED: set NIUCAI_PAIRING_CODE or NIUCAI_NODE_ID/NIUCAI_NODE_TOKEN")
    response = httpx.post(
        kernel_url + "/api/pairing/redeem",
        json={"code": code, "platform": platform(), "runner_version": RUNNER_VERSION},
        timeout=10,
    )
    if response.status_code == 409:
        raise SystemExit("PAIRING_CODE_INVALID: code expired or already used; generate a new one")
    response.raise_for_status()
    value = response.json()
    # 先落盘再使用（mkstemp 为 0600）：配对码只能兑换一次，丢失令牌只能重新配对。
    atomic_json(stored, {"node_id": value["node_id"], "token": value["token"]})
    print(f"PAIRED: node {value['node_id']} ({value['name']})", flush=True)
    return value["node_id"], value["token"], value.get("kernel_instance_id")


def process_start(pid):
    if WINDOWS:
        import win_job

        return win_job.process_start(pid)
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except FileNotFoundError:
        return None


async def kill_group(pid, job=None):
    if WINDOWS:
        # Job Object 结束整棵进程树；重启后恢复台账时没有 Job 句柄，用 taskkill /T 兜底。
        if job is not None:
            job.terminate()
        with contextlib.suppress(OSError):
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/T",
                "/F",
                "/PID",
                str(pid),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                creationflags=0x08000000,
            )
            await killer.wait()
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pid, signal.SIGTERM)
    await asyncio.sleep(0.3)
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pid, signal.SIGKILL)


def job_model(job, default):
    """Bot 级模型覆盖（白板卡片快照 bot.model），为空时用节点默认模型。"""
    snapshot = (job or {}).get("config_snapshot") or {}
    model = str((snapshot.get("bot") or {}).get("model") or "")
    if not model:
        return default
    if not MODEL_NAME.fullmatch(model):
        raise ValueError("invalid bot model name")
    return model


def board_server_config(board):
    """niucai-board MCP 服务器：以引擎身份运行，只经本作业的代理端点访问 Runner。

    `board` 是 Unix socket 路径（Linux），或 {"address": "tcp:127.0.0.1:<port>", "secret": ...}（Windows：
    回环端口 + 每作业密钥）。打包为单文件 exe 时由 Runner 自身以 `board-mcp` 子命令提供。
    """
    address, secret = (
        (board["address"], board.get("secret")) if isinstance(board, dict) else (str(board), None)
    )
    if getattr(sys, "frozen", False):
        config = {"command": sys.executable, "args": ["board-mcp", address]}
    else:
        config = {"command": sys.executable, "args": [str(BOARD_SCRIPT), address]}
    if secret:
        config["env"] = {"NIUCAI_BOARD_SECRET": secret}
    return config


def job_mcp_servers(job):
    """Bot 绑定的外部 MCP 服务器 -> ({name: config}, [跳过原因])。

    Kernel 只下发定义；`env:NAME` 形式的值在这里从节点环境变量读取，密钥不经过 Kernel。
    缺少环境变量或定义不合法的服务器整体跳过；只读档位不加载外部 MCP（其工具可能有副作用）。
    """
    snapshot = (job or {}).get("config_snapshot") or {}
    servers, skipped = {}, []
    definitions = snapshot.get("mcp_servers") or []
    if definitions and (snapshot.get("permission_profile") or "workspace") == "read":
        return {}, [f"{str(d.get('name', ''))[:64]}：只读档位不加载外部 MCP" for d in definitions]
    for item in definitions:
        name = str(item.get("name", ""))
        config = item.get("config") or {}
        if not ASSET_NAME.fullmatch(name) or name == "niucai-board" or name in servers:
            skipped.append(f"{name[:64]}：名称不合法")
            continue
        missing = []

        def resolve(values, key_pattern):
            resolved = {}
            for key, value in (values or {}).items():
                if not key_pattern.fullmatch(str(key)) or not isinstance(value, str):
                    raise ValueError(str(key)[:100])
                if value.startswith("env:"):
                    variable = value[4:]
                    if not ENV_KEY.fullmatch(variable) or variable not in os.environ:
                        missing.append(variable[:100])
                        continue
                    value = os.environ[variable]
                resolved[str(key)] = value
            return resolved

        try:
            if item.get("transport") == "stdio":
                command, args = config.get("command"), config.get("args") or []
                if not isinstance(command, str) or not command or not all(isinstance(a, str) for a in args):
                    raise ValueError("command")
                server = {"command": command, "args": list(args), "env": resolve(config.get("env"), ENV_KEY)}
            else:
                url = config.get("url")
                if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                    raise ValueError("url")
                server = {"url": url, "headers": resolve(config.get("headers"), HEADER_KEY)}
        except ValueError as exc:
            skipped.append(f"{name}：定义不合法（{exc}）")
            continue
        if missing:
            skipped.append(f"{name}：节点缺少环境变量 {', '.join(missing)}")
            continue
        servers[name] = server
    return servers, skipped


def write_skills(root, skills, uid):
    """Bot 绑定的 skill 写到 <root>/<name>/SKILL.md，返回 [(name, description, path)]。"""
    written = []
    for item in skills or []:
        name = str(item.get("name", ""))
        if not ASSET_NAME.fullmatch(name):
            continue
        description = " ".join(str(item.get("description") or "").split())[:300]
        content = str(item.get("content") or "")
        if not content.startswith("---"):
            # Claude Code 识别带 frontmatter 的 SKILL.md；其他引擎按提示中的路径读取。
            summary = json.dumps(description or name, ensure_ascii=False)
            content = f"---\nname: {name}\ndescription: {summary}\n---\n\n{content}"
        path = root / ".claude" / "skills" / name / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        written.append((name, description, path))
    if written:
        own_tree(root, uid)
    return written


def job_timeout(snapshot):
    """卡片的 budget.max_minutes 随快照下发为 timeout_seconds，取代节点默认超时（最长 24 小时）。"""
    value = snapshot.get("timeout_seconds")
    if isinstance(value, int) and not isinstance(value, bool) and 0 < value <= 86400:
        return float(value)
    return float(os.environ.get("NIUCAI_PI_JOB_TIMEOUT", "1800"))


def write_card_inputs(workspace, inputs, uid):
    """依赖卡片的内联文件写到 inputs/<card_id>/，路径不得逃出该目录。"""
    root = workspace / "inputs"
    for item in inputs or []:
        card = str(item.get("card_id", ""))
        if not SAFE_ID.fullmatch(card):
            raise RuntimeError("invalid card input id")
        target = root / card
        for file in item.get("files") or []:
            raw = str(file.get("path", ""))
            parts = raw.split("/")
            if raw.startswith("/") or any(p in {"", ".", ".."} or "\\" in p or ":" in p for p in parts):
                raise RuntimeError("invalid card input path")
            path = target.joinpath(*parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(base64.b64decode(file.get("base64", ""), validate=True))
    if root.exists():
        own_tree(root, uid)


def own_tree(path, uid):
    if uid is None:
        return
    for directory, dirs, files in os.walk(path, followlinks=False):
        os.chown(directory, uid, uid)
        for name in dirs + files:
            candidate = Path(directory) / name
            if not candidate.is_symlink():
                os.chown(candidate, uid, uid)


def resume_session(job):
    """Engine session id of a continuation job, or None; validated before it reaches a command line."""
    resume = ((job or {}).get("config_snapshot") or {}).get("resume")
    if not resume:
        return None
    session = str(resume.get("session_id", ""))
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", session):
        raise ValueError("invalid resume session id")
    return session


class PiEngine:
    """Pi RPC engine: the only engine today. Owns its Unix UID, HOME and CLI protocol."""

    name = "pi"
    label = "Pi"
    closes_stdin = False  # RPC stays open until the process is killed

    def __init__(self, root):
        self.uid = engine_uid("NIUCAI_PI_UID", "10001")
        self.home = root / "pi-home"
        self.home.mkdir(exist_ok=True)
        chown(self.home, self.uid)
        self.provider = os.environ.get("NIUCAI_PI_PROVIDER", "openrouter")
        self.model = os.environ.get("NIUCAI_PI_MODEL", "")
        if not self.model:
            raise ValueError("NIUCAI_PI_MODEL must be configured")
        if os.environ.get("NIUCAI_PI_BASE_URL"):
            atomic_json(
                self.home / "models.json",
                {
                    "providers": {
                        self.provider: {
                            "baseUrl": os.environ["NIUCAI_PI_BASE_URL"],
                            "api": "openai-completions",
                            "apiKey": "${PI_PROVIDER_KEY}",
                            "models": [
                                {
                                    "id": self.model,
                                    "contextWindow": 64000,
                                    "maxTokens": 4096,
                                    "reasoning": False,
                                    "input": ["text"],
                                    "cost": {
                                        "input": 0,
                                        "output": 0,
                                        "cacheRead": 0,
                                        "cacheWrite": 0,
                                    },
                                }
                            ],
                        }
                    }
                },
                owner=self.uid,
            )

    def heartbeat(self):
        return {
            "executor": self.name,
            "available": True,
            "version": "1.1.0",
            "features": ["artifact-v2", "managed-workspace", "card-board"],
            "node_model": {"provider": self.provider, "model": self.model},
        }

    def status(self):
        return {"available": True, "version": "1.1.0", "model": self.model}

    def environment(self):
        # Runner credentials are never inherited by Pi; Pi runs under another Unix UID.
        result = {
            **base_environment(self.home),
            "PI_CODING_AGENT_DIR": str(self.home),
            "PI_PROVIDER_KEY": os.environ.get("NIUCAI_PI_PROVIDER_KEY", ""),
        }
        for name in ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
            if name in os.environ:
                result[name] = os.environ[name]
        return result

    def drop_privileges(self):
        os.setgroups([])
        os.setgid(self.uid)
        os.setuid(self.uid)

    def command(self, session, job=None, board=None, extra_dirs=(), mcp=None):
        # Pi 暂不接 MCP（board 与外部 MCP 均忽略），卡片结果经最终回答提交。
        return [
            "pi",
            "--mode",
            "rpc",
            "--provider",
            self.provider,
            "--model",
            job_model(job, self.model),
            "--session",
            str(session),
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-approve",
            "--no-context-files",
            "--no-mcp",
            "--offline",
        ]

    @staticmethod
    def stdin(attempt, message):
        frames = (
            {"type": "get_state", "id": "state"},
            {"type": "prompt", "id": attempt, "message": message},
        )
        return "".join(json.dumps(frame) + "\n" for frame in frames).encode()

    def translate(self, frame, state):
        """Map one raw frame to (events, final). `final` is the result dict when finished."""
        kind = frame.get("type", "")
        if kind == "response" and not frame.get("success", True):
            raise RuntimeError(str(frame.get("error", "Pi RPC command rejected"))[:500])
        if kind == "response" and frame.get("command") == "get_state":
            data = {"session_id": frame.get("data", {}).get("sessionId", state["job_id"])}
            return [("session", data)], None
        if kind == "message_end" and frame.get("message", {}).get("role") == "assistant":
            message = frame["message"]
            state["failed"] = message.get("stopReason") in {"error", "aborted"}
            state["answer"] = "\n".join(
                b["text"] for b in message.get("content", []) if b.get("type") == "text"
            )
            data = {"text": state["answer"][:4000], "stop_reason": message.get("stopReason")}
            return [("assistant", data)], None
        if kind in {"tool_execution_start", "tool_execution_end"}:
            data = {
                "tool": str(frame.get("toolName", ""))[:100],
                "is_error": bool(frame.get("isError")),
            }
            return [(kind, data)], None
        if kind in {"agent_end", "agent_settled"}:
            if not state["answer"] or state["failed"]:
                raise RuntimeError("Pi ended without a successful final answer")
            return [], {
                "answer": state["answer"][:16000],
                "executor": self.name,
                "session_path": state["session_path"],
            }
        return [], None


class CodexEngine:
    """Codex CLI engine: `codex exec --json -`, prompt on stdin, API-key auth only."""

    name = "codex"
    label = "Codex"
    closes_stdin = True  # `exec -` reads the prompt until EOF
    sandboxes = {"read": "read-only", "workspace": "workspace-write", "full": "danger-full-access"}
    ignored_items = {"reasoning", "todo_list", "agent_message", "error"}  # item "error" = CLI warning
    tools = {
        "command_execution": "shell",
        "file_change": "edit",
        "mcp_tool_call": "mcp",
        "web_search": "web_search",
    }

    def __init__(self, root):
        self.uid = engine_uid("NIUCAI_CODEX_UID", "10002")
        self.home = root / "codex-home"
        self.home.mkdir(exist_ok=True)
        chown(self.home, self.uid)
        self.model = os.environ.get("NIUCAI_CODEX_MODEL", "")
        self.base_url = os.environ.get("NIUCAI_CODEX_BASE_URL", "")
        allowed = {a.strip() for a in os.environ.get("NIUCAI_CODEX_PROFILES", "read,workspace").split(",")}
        self.version = self.probe_version()
        # Codex's own sandbox needs unprivileged user namespaces, which hardened containers deny.
        self.sandbox = self.probe_sandbox()
        self.profiles = [
            p
            for p in ("read", "workspace", "full")
            if p in allowed
            and (self.sandbox == "engine" or p != "read")  # read-only cannot be enforced
            # Windows 没有容器兜底：引擎沙箱不可用时只允许显式开放的 full。
            and (not WINDOWS or self.sandbox == "engine" or p == "full")
        ]

    @staticmethod
    def probe_version():
        if not shutil.which("codex"):
            return ""
        try:
            output = subprocess.run(
                ["codex", "--version"], capture_output=True, text=True, timeout=10, check=False
            ).stdout.split()
        except (OSError, subprocess.SubprocessError):
            return ""
        return output[-1][:100] if output else ""

    def probe_sandbox(self):
        if not shutil.which("codex"):
            return "container"
        # Windows 沙箱子命令以锁定的 CLI 版本实测为准；探测失败即视为不可用。
        probe = (
            ["codex", "sandbox", "windows", "cmd", "/c", "exit", "0"]
            if WINDOWS
            else ["codex", "sandbox", "--", "/bin/true"]
        )
        try:
            probe = subprocess.run(
                resolve_command(probe),
                capture_output=True,
                timeout=30,
                check=False,
                cwd=self.home,
                env=self.environment(),
                preexec_fn=None if WINDOWS else self.drop_privileges,
                creationflags=0x08000000 if WINDOWS else 0,
            )
        except (OSError, RuntimeError, subprocess.SubprocessError):
            return "container"
        return "engine" if probe.returncode == 0 else "container"

    @staticmethod
    def credential():
        return os.environ.get("NIUCAI_CODEX_API_KEY", "")

    def status(self):
        if not shutil.which("codex"):
            return {"available": False, "reason": "not_installed"}
        if not self.credential() and not WINDOWS:  # Windows 默认复用用户已登录的 Codex 账号
            return {"available": False, "reason": "credential_missing"}
        return {
            "available": True,
            "version": self.version,
            "model": self.model,
            "profiles": self.profiles,
            "sandbox": self.sandbox,
        }

    def environment(self):
        # Only Codex's own credential; the node token and Pi keys never reach this process.
        if WINDOWS:
            # 复用当前用户的 ~/.codex 登录；配置了 API Key 时才注入。
            environment = base_environment(self.home)
            if self.credential():
                environment["CODEX_API_KEY"] = self.credential()
            return environment
        return {
            **base_environment(self.home),
            "CODEX_HOME": str(self.home),
            "CODEX_API_KEY": self.credential(),
        }

    def drop_privileges(self):
        os.setgroups([])
        os.setgid(self.uid)
        os.setuid(self.uid)

    def command(self, session, job=None, board=None, extra_dirs=(), mcp=None):
        profile = ((job or {}).get("config_snapshot") or {}).get("permission_profile") or "workspace"
        if profile not in self.sandboxes or profile not in self.profiles:
            raise ValueError(f"permission profile not enabled on this node: {str(profile)[:40]}")
        # Without the engine sandbox the container is the boundary; `workspace` is labelled as such.
        sandbox = self.sandboxes[profile] if self.sandbox == "engine" else "danger-full-access"
        resume = resume_session(job)
        if resume:
            # `codex exec resume` has no --sandbox flag; the same policy goes through configuration.
            head = [
                "codex",
                "exec",
                "resume",
                "--json",
                "--skip-git-repo-check",
                "-c",
                f'sandbox_mode="{sandbox}"',
            ]
        else:
            head = ["codex", "exec", "--json", "--skip-git-repo-check", "--sandbox", sandbox]
        args = [
            *head,
            "--output-last-message",
            str(session.with_suffix(".txt")),
            "-c",
            'approval_policy="never"',
            "-c",
            "project_doc_max_bytes=0",  # workspace AGENTS.md is untrusted input
            "-c",
            'web_search="disabled"',
        ]
        if model := job_model(job, self.model):
            args += ["-m", model]
        if board:
            server = board_server_config(board)
            args += [
                "-c",
                "mcp_servers.niucai-board.command=" + json.dumps(server["command"]),
                "-c",
                "mcp_servers.niucai-board.args=" + json.dumps(server["args"]),
            ]
            for key, value in (server.get("env") or {}).items():
                args += ["-c", f"mcp_servers.niucai-board.env.{key}=" + json.dumps(value)]
        for name, server in (mcp or {}).items():
            # 已知限制：Codex 只能经 -c 传入，解析后的密钥会出现在命令行参数中。
            prefix = f"mcp_servers.{name}"
            if "url" in server:
                args += ["-c", f"{prefix}.url=" + json.dumps(server["url"])]
                for key, value in server["headers"].items():
                    args += ["-c", f'{prefix}.http_headers."{key}"=' + json.dumps(value)]
            else:
                args += [
                    "-c",
                    f"{prefix}.command=" + json.dumps(server["command"]),
                    "-c",
                    f"{prefix}.args=" + json.dumps(server["args"]),
                ]
                for key, value in server["env"].items():
                    args += ["-c", f"{prefix}.env.{key}=" + json.dumps(value)]
        if self.base_url:
            provider = "model_providers.niucai"
            args += [
                "-c",
                'model_provider="niucai"',
                "-c",
                f'{provider}.name="niucai"',
                "-c",
                f"{provider}.base_url=" + json.dumps(self.base_url),
                "-c",
                f'{provider}.env_key="CODEX_API_KEY"',
                "-c",
                f'{provider}.wire_api="responses"',
            ]
        return [*args, *([resume] if resume else []), "-"]

    @staticmethod
    def stdin(attempt, message):
        return message.encode()

    @staticmethod
    def tool_failed(item):
        status = item.get("status")
        if item.get("type") == "command_execution":
            return item.get("exit_code") not in (0, None) or status == "failed"
        if item.get("type") == "mcp_tool_call":
            return status == "failed" or bool(item.get("error"))
        return status == "failed"

    def translate(self, frame, state):
        """Map one `codex exec --json` frame to (events, final), like PiEngine.translate."""
        kind = frame.get("type", "")
        if kind == "thread.started":
            state["thread_id"] = str(frame.get("thread_id", ""))
            return [("session", {"session_id": state["thread_id"] or state["job_id"]})], None
        if kind in {"item.started", "item.updated", "item.completed"}:
            item = frame.get("item") or {}
            item_type = item.get("type", "")
            if item_type == "agent_message" and kind == "item.completed":
                state["answer"] = str(item.get("text", ""))
                data = {"text": state["answer"][:4000], "stop_reason": "stop"}
                return [("assistant", data)], None
            tool = self.tools.get(item_type)
            if tool is None:
                if item_type not in self.ignored_items:
                    state["unknown_frames"] = state.get("unknown_frames", 0) + 1
                return [], None
            opened = state.setdefault("open", set())
            events = []
            if item.get("id") not in opened and kind != "item.updated":
                opened.add(item.get("id"))
                events.append(("tool_execution_start", {"tool": tool, "is_error": False}))
            if kind == "item.completed":
                opened.discard(item.get("id"))
                events.append(("tool_execution_end", {"tool": tool, "is_error": self.tool_failed(item)}))
            return events, None
        if kind == "turn.failed":
            raise RuntimeError(str((frame.get("error") or {}).get("message") or "Codex turn failed")[:500])
        if kind == "error":
            message = str(frame.get("message", ""))
            if message.startswith("Reconnecting"):  # transient stream retry, not a failure
                return [], None
            raise RuntimeError(message[:500] or "Codex error")
        if kind == "turn.completed":
            answer = state["answer"]
            if not answer:
                with contextlib.suppress(OSError):
                    answer = Path(state["session_path"]).with_suffix(".txt").read_text()
            if not answer:
                raise RuntimeError("Codex ended without a final answer")
            final = {
                "answer": answer[:16000],
                "executor": self.name,
                "session_id": state.get("thread_id", ""),
            }
            if isinstance(frame.get("usage"), dict):
                final["usage"] = {k: v for k, v in frame["usage"].items() if isinstance(v, int)}
            if state.get("unknown_frames"):
                final["unknown_frames"] = state["unknown_frames"]
            return [], final
        if kind != "turn.started":
            state["unknown_frames"] = state.get("unknown_frames", 0) + 1
        return [], None


class ClaudeEngine:
    """Claude Code engine: `claude -p --output-format stream-json`, prompt on stdin, API-key auth only."""

    name = "claude"
    label = "Claude Code"
    closes_stdin = True  # `-p` reads the prompt until EOF
    sandbox = "container"  # no OS sandbox is used here; the node container is the boundary
    web_tools = ["WebSearch", "WebFetch"]
    ignored_blocks = {"thinking", "redacted_thinking"}
    # Frame types without job-relevant information; any other type is counted as format drift.
    ignored_frames = {"system", "stream_event", "rate_limit_event"}

    def __init__(self, root):
        self.uid = engine_uid("NIUCAI_CLAUDE_UID", "10003")
        self.home = root / "claude-home"
        self.home.mkdir(exist_ok=True)
        chown(self.home, self.uid)
        self.model = os.environ.get("NIUCAI_CLAUDE_MODEL", "")
        self.base_url = os.environ.get("NIUCAI_CLAUDE_BASE_URL", "")
        self.budget = os.environ.get("NIUCAI_CLAUDE_MAX_BUDGET_USD", "")
        allowed = {a.strip() for a in os.environ.get("NIUCAI_CLAUDE_PROFILES", "read,workspace").split(",")}
        self.profiles = [p for p in ("read", "workspace", "full") if p in allowed]
        self.version = self.probe_version()

    @staticmethod
    def probe_version():
        if not shutil.which("claude"):
            return ""
        try:
            output = subprocess.run(
                ["claude", "--version"], capture_output=True, text=True, timeout=30, check=False
            ).stdout.split()
        except (OSError, subprocess.SubprocessError):
            return ""
        return output[0][:100] if output else ""

    @staticmethod
    def credential():
        return os.environ.get("NIUCAI_CLAUDE_API_KEY", "")

    def status(self):
        if not shutil.which("claude"):
            return {"available": False, "reason": "not_installed"}
        if not self.credential() and not WINDOWS:  # Windows 默认复用用户已登录的 Claude Code 账号
            return {"available": False, "reason": "credential_missing"}
        return {
            "available": True,
            "version": self.version,
            "model": self.model,
            "profiles": self.profiles,
            "sandbox": self.sandbox,
        }

    def environment(self):
        # Only Claude's own credential; the node token and Pi keys never reach this process.
        environment = {
            **base_environment(self.home),
            "DISABLE_AUTOUPDATER": "1",
            "DISABLE_TELEMETRY": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        }
        if not WINDOWS:
            environment["CLAUDE_CONFIG_DIR"] = str(self.home)
            environment["ANTHROPIC_API_KEY"] = self.credential()
        elif self.credential():  # Windows 未配置 API Key 时复用 ~/.claude 的登录
            environment["ANTHROPIC_API_KEY"] = self.credential()
        if self.base_url:
            environment["ANTHROPIC_BASE_URL"] = self.base_url
        return environment

    def drop_privileges(self):
        os.setgroups([])
        os.setgid(self.uid)
        os.setuid(self.uid)

    def command(self, session, job=None, board=None, extra_dirs=(), mcp=None):
        profile = ((job or {}).get("config_snapshot") or {}).get("permission_profile") or "workspace"
        if profile not in self.profiles:
            raise ValueError(f"permission profile not enabled on this node: {str(profile)[:40]}")
        servers = {"niucai-board": board_server_config(board)} if board else {}
        for name, server in (mcp or {}).items():
            servers[name] = (
                {"type": "http", "url": server["url"], "headers": server["headers"]}
                if "url" in server
                else {"command": server["command"], "args": server["args"], "env": server["env"]}
            )
        config_json = json.dumps({"mcpServers": servers})
        if WINDOWS or mcp:
            # Windows 参数要经过 claude.cmd 垫片；外部 MCP 可能带密钥，不放进命令行。JSON 写到会话旁的文件。
            config = Path(session).with_suffix(".mcp.json")
            config.write_text(config_json, encoding="utf-8")
            if not WINDOWS:
                os.chmod(config, 0o600)
                chown(config, self.uid)
            config_json = str(config)
        args = ["claude", "-p", "--output-format", "stream-json", "--verbose"]
        if not WINDOWS or self.credential():
            # --bare skips CLAUDE.md discovery, hooks, plugins, keychain and auto-memory: the workspace
            # and the node's home are untrusted input, only the flags below configure the run.
            # Windows 复用已登录账号时不能用 --bare（它不读取登录凭据）。
            args.append("--bare")
        args += ["--strict-mcp-config", "--mcp-config", config_json, "--disallowedTools", *self.web_tools]
        for directory in extra_dirs:
            args += ["--add-dir", str(directory)]
        if resume := resume_session(job):
            args += ["--resume", resume]
        board_tools = [f"mcp__{name}" for name in servers]
        if profile == "read":
            # Read-only is enforced by removing every tool that can write or run commands.
            args += ["--tools", "Read,Glob,Grep", "--permission-mode", "dontAsk"]
            if board_tools:
                args += ["--allowedTools", *board_tools]
        elif profile == "workspace":
            args += [
                "--permission-mode",
                "acceptEdits",
                "--allowedTools",
                "Bash",
                "Edit",
                "Write",
                "Read",
                *board_tools,
            ]
        else:
            args += ["--permission-mode", "bypassPermissions"]
        if model := job_model(job, self.model):
            args += ["--model", model]
        if self.budget:
            args += ["--max-budget-usd", self.budget]
        return args

    @staticmethod
    def stdin(attempt, message):
        return message.encode()

    def translate(self, frame, state):
        """Map one `claude -p --output-format stream-json` frame to (events, final)."""
        kind = frame.get("type", "")
        if kind == "system":
            if frame.get("subtype") == "init":
                state["thread_id"] = str(frame.get("session_id", ""))
                return [("session", {"session_id": state["thread_id"] or state["job_id"]})], None
            return [], None
        if kind in {"user", "assistant"}:
            if frame.get("parent_tool_use_id"):  # sub-agent traffic is not reported
                return [], None
            events = []
            for block in (frame.get("message") or {}).get("content") or []:
                block_type = block.get("type") if isinstance(block, dict) else None
                if kind == "user" and block_type == "tool_result":
                    tool = state.setdefault("tools", {}).pop(block.get("tool_use_id"), "tool")
                    events.append(
                        ("tool_execution_end", {"tool": tool, "is_error": bool(block.get("is_error"))})
                    )
                elif kind == "assistant" and block_type == "text":
                    state["answer"] = str(block.get("text", ""))
                    data = {"text": state["answer"][:4000], "stop_reason": "stop"}
                    events.append(("assistant", data))
                elif kind == "assistant" and block_type == "tool_use":
                    tool = str(block.get("name", "tool"))[:64]
                    state.setdefault("tools", {})[block.get("id")] = tool
                    events.append(("tool_execution_start", {"tool": tool, "is_error": False}))
                elif block_type not in self.ignored_blocks and not (kind == "user" and block_type == "text"):
                    state["unknown_frames"] = state.get("unknown_frames", 0) + 1
            return events, None
        if kind == "result":
            answer = str(frame.get("result") or state["answer"])
            if frame.get("is_error") or frame.get("subtype") != "success":
                reason = answer or str(frame.get("subtype") or "error")
                raise RuntimeError(f"Claude Code failed ({frame.get('subtype')}): {reason}"[:500])
            if not answer:
                raise RuntimeError("Claude Code ended without a final answer")
            final = {
                "answer": answer[:16000],
                "executor": self.name,
                "session_id": str(frame.get("session_id") or state.get("thread_id", "")),
            }
            usage = {k: v for k, v in (frame.get("usage") or {}).items() if isinstance(v, int)}
            if isinstance(frame.get("num_turns"), int):
                usage["num_turns"] = frame["num_turns"]
            if usage:
                final["usage"] = usage
            if isinstance(frame.get("total_cost_usd"), (int, float)):
                final["cost_usd"] = round(float(frame["total_cost_usd"]), 6)
            if state.get("unknown_frames"):
                final["unknown_frames"] = state["unknown_frames"]
            return [], final
        if kind not in self.ignored_frames:
            state["unknown_frames"] = state.get("unknown_frames", 0) + 1
        return [], None


class Runner:
    def __init__(self):
        self.root = Path(os.environ.get("NIUCAI_NODE_DATA") or default_data_dir())
        self.work = self.root / ("work" if WINDOWS else "workspaces")
        self.control = self.root / "control"
        self.control.mkdir(parents=True, exist_ok=True, mode=0o700)
        # 每个执行中的 Attempt 一份台账（control/active/<job>.json），支持多槽位并发。
        self.ledgers = self.control / "active"
        self.ledgers.mkdir(exist_ok=True, mode=0o700)
        self.board_dir = self.root / "board"
        self.board_dir.mkdir(exist_ok=True)
        if not WINDOWS:
            os.chmod(self.board_dir, 0o711)  # 引擎 UID 可进入但不能列出；socket 本身 0600 归引擎 UID
        self.tasks = {}
        self.draining = False
        self.offline_grace = float(os.environ.get("NIUCAI_OFFLINE_GRACE", "240"))
        self.lock = (self.control / "runner.lock").open("a+")
        if WINDOWS:
            import msvcrt

            try:
                msvcrt.locking(self.lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise SystemExit("RUNNER_ALREADY_RUNNING: another runner uses this data directory") from None
        else:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        kernel_url = os.environ["NIUCAI_KERNEL_URL"].rstrip("/")
        self.node_id, token, paired_identity = node_credentials(self.control, kernel_url)
        identity_file = self.root / "kernel-identity.json"
        self.identity_file = identity_file
        if paired_identity and not identity_file.exists():
            atomic_json(identity_file, {"kernel_instance_id": paired_identity})
        self.kernel_instance_id = os.environ.get("NIUCAI_KERNEL_INSTANCE_ID") or (
            json.loads(identity_file.read_text()).get("kernel_instance_id")
            if identity_file.exists()
            else None
        )
        self.prefix = f"/api/remote/runner/{self.node_id}"
        self.client = httpx.AsyncClient(
            base_url=kernel_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=5,
        )
        default_engines = "claude,codex" if WINDOWS else "pi"
        self.requested = {e.strip() for e in os.environ.get("NIUCAI_ENGINES", default_engines).split(",")}
        # Linux 节点始终带 Pi（旧协议的默认引擎）；Windows 只启用请求的引擎。
        engines = [PiEngine(self.root)] if "pi" in self.requested or not WINDOWS else []
        if "codex" in self.requested:
            engines.append(CodexEngine(self.root))
        if "claude" in self.requested:
            engines.append(ClaudeEngine(self.root))
        self.engines = {engine.name: engine for engine in engines}

    async def post(self, suffix, data=None, **options):
        response = await self.client.post(self.prefix + suffix, json=data, **options)
        response.raise_for_status()
        return response.json()

    def migrate_ledger(self):
        """P1 及更早版本的单一台账 control/active.json 迁入按作业的目录。"""
        legacy = self.control / "active.json"
        if legacy.exists():
            record = json.loads(legacy.read_text())
            if SAFE_ID.fullmatch(str(record.get("id", ""))):
                atomic_json(self.ledgers / f"{record['id']}.json", record)
            legacy.unlink()

    async def replay_events(self, record, events):
        """按序补交台账旁事件日志里尚未确认的事件；Kernel 按序号去重，重复补交无害。"""
        if not events.exists():
            return
        acked = int(record.get("acked") or 0)
        for line in events.read_text(encoding="utf-8").splitlines():
            try:
                payload = json.loads(line)
            except ValueError:
                break  # 崩溃时写了一半的最后一行
            if payload.get("attempt_id") != record["attempt_id"] or payload["sequence"] <= acked:
                continue
            try:
                await self.post(f"/jobs/{record['id']}/events", payload, timeout=30)
            except httpx.HTTPStatusError as exc:
                # 409/404：作业已不在运行或序号断档，Kernel 不再接收事件，直接交结论。
                if exc.response.status_code in {404, 409}:
                    return
                raise
            acked = payload["sequence"]
            record["acked"] = acked

    async def finish_ledger(self, ledger):
        record = json.loads(ledger.read_text())
        events = ledger.with_suffix(".events.jsonl")
        pid = record.get("pid")
        if pid and process_start(pid) == record.get("process_start"):
            await kill_group(pid)
        # finish 之后 Kernel 拒收事件，所以先补齐断网或崩溃时没送达的进度事件。
        await self.replay_events(record, events)
        # Always reconcile the old attempt before claiming another job.
        result = record.get(
            "finish",
            {
                "status": "FAILED",
                "result": {
                    "error": "Runner restarted; previous process stopped; retry explicitly to resume session"
                },
            },
        )
        try:
            await self.post(
                f"/jobs/{record['id']}/finish",
                {"attempt_id": record["attempt_id"], **result},
                timeout=30,
            )
        except httpx.HTTPStatusError as exc:
            # 409/404：Attempt 已被 Kernel 收回或不存在，结论无处可交，丢弃台账。
            if exc.response.status_code not in {404, 409}:
                raise
        ledger.unlink()
        events.unlink(missing_ok=True)

    async def reconcile(self):
        """交付不在执行中的台账：重启前的进程、或断网时没交出去的结果。"""
        for ledger in sorted(self.ledgers.glob("*.json")):
            if ledger.stem not in self.tasks:
                await self.finish_ledger(ledger)

    def heartbeat_payload(self):
        executors = {name: engine.status() for name, engine in self.engines.items()}
        if "pi" in self.engines:
            head = self.engines["pi"].heartbeat()
        else:
            head = {
                "executor": next(iter(self.engines), "none"),
                "available": any(e.get("available") for e in executors.values()),
                "version": RUNNER_VERSION,
                "features": ["artifact-v2", "managed-workspace", "card-board"],
            }
        allow = [str(p) for p in workdir_allowlist()][:20]
        return {
            **head,
            "protocol_version": 3,
            "executors": executors,
            "kernel_instance_id": self.kernel_instance_id,
            "platform": platform(),
            "runner_version": RUNNER_VERSION,
            **({"workdir_allow": allow} if allow else {}),
            # 画面预览由 Desktop 直连节点；地址由部署者配置，Kernel 只记录不代理。
            **({"preview_url": url} if (url := os.environ.get("NIUCAI_PREVIEW_URL")) else {}),
        }

    @staticmethod
    def compose_prompt(job, inputs=None, skills=(), skipped=()):
        snapshot = job.get("config_snapshot") or {}
        assets = ""
        if skills:
            assets = "可用技能（需要时先读对应 SKILL.md 再照做）：\n" + "\n".join(
                f"- {name}：{description or '（无描述）'} —— {path}" for name, description, path in skills
            )
        if skipped:
            assets += ("\n" if assets else "") + "以下 MCP 服务器本次未加载：" + "；".join(skipped)
        if job.get("card_id"):
            # 白板卡片：Kernel 已拼好卡片提示；这里只前置 Bot 身份与指令，不回显内联输入。
            bot = snapshot.get("bot") or {}
            head = f"你是 Bot @{bot.get('name', '')}" + (f"，角色：{bot['role']}" if bot.get("role") else "")
            # 指定本机目录时依赖卡片的输入不放进用户目录，提示其所在位置。
            note = f"依赖卡片的输入文件在 {inputs}（只读参考）。" if inputs else ""
            parts = [head, snapshot.get("instructions") or "", assets, note, job["prompt"]]
            return "\n\n".join(p for p in parts if p)
        visible = {k: v for k, v in snapshot.items() if k not in {"skills", "mcp_servers"}}
        return (
            "Work in the assigned workspace. Return evidence and file outputs.\n"
            + "Bot role and delivery configuration:\n"
            + json.dumps(visible, ensure_ascii=False)
            + "\n"
            + (assets + "\n" if assets else "")
            + job["prompt"]
            + (
                "\nAuthorized input artifact IDs are materialized under inputs/. "
                + json.dumps(job["input_artifacts"])
                if job.get("input_artifacts")
                else ""
            )
        )

    async def board_call(self, job, tool, args):
        """引擎侧 MCP 工具调用经 Runner 转发；板令牌与节点令牌都不进入引擎进程。"""
        if not BOARD_TOOL.fullmatch(str(tool)) or not isinstance(args, dict):
            return {"ok": False, "error": "invalid board call"}
        url = f"{self.prefix}/jobs/{job['id']}/board/{tool}"
        for count in range(3):
            try:
                response = await self.client.post(
                    url, json={"args": args}, headers={"X-Board-Token": job["board_token"]}, timeout=15
                )
            except httpx.HTTPError as exc:
                if count == 2:
                    return {"ok": False, "error": f"kernel unreachable: {type(exc).__name__}"}
                await asyncio.sleep(1)
                continue
            if response.status_code >= 400:
                try:
                    detail = response.json().get("detail", response.text)
                except ValueError:
                    detail = response.text
                return {"ok": False, "error": str(detail)[:500]}
            return {"ok": True, "result": response.json()}

    async def board_server(self, job, uid):
        secret = secrets.token_urlsafe(24) if WINDOWS else None

        async def handle(reader, writer):
            try:
                while line := await reader.readline():
                    try:
                        request = json.loads(line)
                        if secret and not hmac.compare_digest(str(request.get("secret", "")), secret):
                            writer.write(b'{"ok": false, "error": "unauthorized"}\n')
                            await writer.drain()
                            break
                        reply = await self.board_call(job, request.get("tool"), request.get("args") or {})
                    except (ValueError, AttributeError):
                        reply = {"ok": False, "error": "malformed request"}
                    writer.write((json.dumps(reply, ensure_ascii=False) + "\n").encode())
                    await writer.drain()
            except (ConnectionError, asyncio.LimitOverrunError, ValueError):
                pass
            finally:
                writer.close()

        if WINDOWS:
            # 没有 Unix socket 的文件权限隔离：只监听回环地址，并要求每作业密钥。
            server = await asyncio.start_server(handle, "127.0.0.1", 0, limit=64 * 1024)
            port = server.sockets[0].getsockname()[1]
            return server, {"address": f"tcp:127.0.0.1:{port}", "secret": secret}
        path = self.board_dir / f"{job['id']}.sock"
        path.unlink(missing_ok=True)
        server = await asyncio.start_unix_server(handle, path=str(path), limit=64 * 1024)
        os.chown(path, uid, uid)
        os.chmod(path, 0o600)
        return server, path

    def workspace_for(self, job):
        """卡片 Attempt 共用 card-<id> 工作区；独立作业的续接沿用上一作业的工作区。"""
        ident = job["id"]
        snapshot = job.get("config_snapshot") or {}
        resume = snapshot.get("resume")
        named = str(snapshot.get("workspace") or "")
        if named:
            if not CARD_WORKSPACE.fullmatch(named):
                raise RuntimeError("invalid card workspace")
            workspace = self.work / named
            if resume and not workspace.is_dir():
                raise RuntimeError("card workspace is gone; cannot resume")
            return workspace
        previous = str(resume.get("job_id", "")) if resume else ""
        # A continuation reuses the previous job's workspace: the engine session is keyed by its path.
        workspace = self.work / (previous if SAFE_ID.fullmatch(previous) else ident)
        if resume and (workspace.name == ident or not workspace.is_dir()):
            raise RuntimeError("workspace of the previous job is gone; cannot resume")
        return workspace

    def start(self, job):
        task = asyncio.create_task(self.execute(job))
        self.tasks[job["id"]] = task
        task.add_done_callback(lambda _: self.tasks.pop(job["id"], None))

    async def execute(self, job):
        ident, attempt = job["id"], job["attempt_id"]
        if not SAFE_ID.fullmatch(str(ident)):
            print("Refusing job with invalid id", flush=True)
            return
        base = f"/jobs/{ident}"
        ledger = self.ledgers / f"{ident}.json"
        record = {"id": ident, "attempt_id": attempt}
        atomic_json(ledger, record)  # Durable admission before starting a process.
        engine = self.engines.get(job.get("engine") or "pi")
        try:
            if engine is None or not engine.status()["available"]:
                raise RuntimeError(f"engine not available on this node: {job.get('engine')}"[:500])
            workspace = self.workspace_for(job)
            snapshot = job.get("config_snapshot") or {}
            # 卡片可指定本机已有目录（P4）：引擎在该目录内工作，不改属主、不回传文件。
            workdir = allowed_workdir(snapshot["workdir"]) if snapshot.get("workdir") else None
        except RuntimeError as exc:
            record["finish"] = {"status": "FAILED", "result": {"error": str(exc)[:500]}}
            atomic_json(ledger, record)
            with contextlib.suppress(httpx.HTTPError):
                await self.finish_ledger(ledger)
            return
        uid = engine.uid
        sessions = self.root / "sessions"
        workspace.mkdir(parents=True, exist_ok=True)
        sessions.mkdir(exist_ok=True)
        if uid is not None and workspace.stat().st_uid != uid:
            own_tree(workspace, uid)  # 同一卡片换了引擎（不同 UID）时接管已有产物
        chown(sessions, uid)
        session = sessions / f"{ident}.jsonl"
        assets = self.root / "assets" / ident
        process = None
        heartbeat = None
        flusher = None
        board = None
        job_object = None
        stopped = asyncio.Event()
        sequence = 0
        outbox = asyncio.Queue()
        # 事件先追加到台账旁的日志再入发件箱：Runner 崩溃或断网超过等待时间时，由 finish_ledger 补交。
        event_log = ledger.with_suffix(".events.jsonl").open("w", encoding="utf-8")
        outcome = {
            "status": "FAILED",
            "result": {"error": f"{engine.label} did not produce a final answer"},
        }
        last_lease = time.monotonic()
        lease_seconds = 5
        card = bool(job.get("card_id"))

        def tolerance():
            # 卡片 Attempt 可离线运行一段时间（Kernel 侧有 LOST 宽限期），其余作业在租约到期前停止。
            return self.offline_grace if card else max(1, lease_seconds - 6)

        async def renew():
            nonlocal last_lease, lease_seconds
            while True:
                try:
                    await self.post("/heartbeat", self.heartbeat_payload())
                    response = await self.post(base + "/heartbeat", {"attempt_id": attempt})
                    if response["stop"]:
                        stopped.set()
                        return
                    last_lease, lease_seconds = (
                        time.monotonic(),
                        response["lease_seconds"],
                    )
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code in {404, 409}:  # Attempt 已被收回
                        stopped.set()
                        return
                    if time.monotonic() - last_lease >= tolerance():
                        stopped.set()
                        return
                except httpx.HTTPError:
                    # Stop well before the last acknowledged lease expires.
                    if time.monotonic() - last_lease >= tolerance():
                        stopped.set()
                        return
                await asyncio.sleep(min(2, lease_seconds / 4))

        async def flush():
            # 事件按序号逐条送达；断网时留在发件箱重试，Kernel 按序号去重。
            delay = 0.2
            while True:
                payload = await outbox.get()
                while True:
                    try:
                        await self.post(base + "/events", payload)
                        delay = 0.2
                        record["acked"] = payload["sequence"]
                        if outbox.empty():
                            atomic_json(ledger, record)  # 确认游标可以滞后：补交时 Kernel 会去重
                        break
                    except httpx.HTTPStatusError as exc:
                        if exc.response.status_code in {404, 409}:
                            stopped.set()
                            break
                        await asyncio.sleep(delay)
                    except httpx.HTTPError:
                        await asyncio.sleep(delay)
                    delay = min(5, delay * 2)
                outbox.task_done()

        def progress(kind, data):
            nonlocal sequence
            sequence += 1
            payload = {
                "attempt_id": attempt,
                "sequence": sequence,
                "kind": kind,
                "data": {**data, "engine": engine.name},
            }
            event_log.write(json.dumps(payload, ensure_ascii=False) + "\n")
            event_log.flush()
            outbox.put_nowait(payload)

        async def read_events():
            nonlocal outcome
            state = {
                "job_id": ident,
                "answer": "",
                "failed": False,
                "session_path": str(session),
            }
            while raw := await process.stdout.readline():
                events, final = engine.translate(json.loads(raw), state)
                for kind, data in events:
                    progress(kind, data)
                if final is not None:
                    outcome = {"status": "COMPLETED", "result": final}
                    return
            raise RuntimeError(f"{engine.label} exited before completion")

        try:
            first = await self.post(base + "/heartbeat", {"attempt_id": attempt})
            if first["stop"]:
                raise RuntimeError("execution permission revoked before launch")
            lease_seconds = first["lease_seconds"]
            heartbeat = asyncio.create_task(renew())
            flusher = asyncio.create_task(flush())
            write_card_inputs(workspace, (job.get("config_snapshot") or {}).get("card_inputs"), uid)
            for reference in job.get("input_artifacts", []):
                if stopped.is_set():
                    raise RuntimeError("artifact download cancelled")
                if not SAFE_ID.fullmatch(reference["id"]):
                    raise RuntimeError("invalid input artifact id")
                inputs = workspace / "inputs"
                inputs.mkdir(exist_ok=True)
                chown(inputs, uid)
                path = inputs / reference["id"]
                digest, size = sha256(), 0
                async with self.client.stream(
                    "GET",
                    self.prefix + base + "/artifacts/" + reference["id"],
                    params={"attempt_id": attempt},
                ) as response:
                    response.raise_for_status()
                    with path.open("wb") as output:
                        async for content in response.aiter_bytes():
                            size += len(content)
                            if size > 20 * 1024 * 1024 or stopped.is_set():
                                raise RuntimeError("input artifact exceeds limit or cancelled")
                            digest.update(content)
                            output.write(content)
                if digest.hexdigest() != reference["content_hash"]:
                    path.unlink(missing_ok=True)
                    raise RuntimeError("input artifact hash mismatch")
                chown(path, uid)
            socket_path = None
            if job.get("board_token") and engine.name != "pi":
                board, socket_path = await self.board_server(job, uid)
            inputs = workspace / "inputs" if workdir and (workspace / "inputs").exists() else None
            snapshot = job.get("config_snapshot") or {}
            # P5：Bot 绑定的 skill 落到本作业的资产目录，结束后删除；MCP 只给支持的引擎。
            skills = write_skills(assets, snapshot.get("skills"), uid)
            mcp, skipped = ({}, []) if engine.name == "pi" else job_mcp_servers(job)
            for reason in skipped:
                progress("notice", {"text": f"MCP 未加载：{reason}"})
            extra_dirs = [d for d in (inputs, assets if skills else None) if d]
            args = engine.command(session, job, socket_path, extra_dirs=extra_dirs, mcp=mcp)
            if WINDOWS:
                import win_job

                spawn = {"creationflags": win_job.CREATE_NEW_PROCESS_GROUP | win_job.CREATE_NO_WINDOW}
            else:
                spawn = {"start_new_session": True, "preexec_fn": engine.drop_privileges}
            process = await asyncio.create_subprocess_exec(
                *resolve_command(args),
                cwd=workdir or workspace,
                env=engine.environment(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=2 * 1024 * 1024,
                **spawn,
            )
            if WINDOWS:
                # 进程启动后立即加入 Job：Runner 退出时句柄关闭，整棵进程树随之结束。
                job_object = win_job.JobObject()
                job_object.assign(process.pid)
            record.update(pid=process.pid, process_start=process_start(process.pid))
            atomic_json(ledger, record)
            process.stdin.write(engine.stdin(attempt, self.compose_prompt(job, inputs, skills, skipped)))
            await process.stdin.drain()
            if engine.closes_stdin:
                process.stdin.close()
            reader = asyncio.create_task(read_events())
            stop_wait = asyncio.create_task(stopped.wait())
            try:
                done, _ = await asyncio.wait(
                    {reader, stop_wait},
                    timeout=job_timeout(snapshot),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if reader in done:
                    await reader
                elif stopped.is_set():
                    outcome = {
                        "status": "CANCELLED",
                        "result": {"error": "cancelled or lease revoked"},
                    }
                else:
                    raise TimeoutError(f"remote {engine.label} job execution timeout")
            finally:
                reader.cancel()
                stop_wait.cancel()
                await asyncio.gather(reader, stop_wait, return_exceptions=True)
        except Exception as exc:
            outcome = {"status": "FAILED", "result": {"error": str(exc)[:500]}}
        finally:
            if process:
                # Even on success stop background descendants before releasing the slot.
                await kill_group(process.pid, job_object)
                await process.wait()
            if job_object:
                job_object.close()
            shutil.rmtree(assets, ignore_errors=True)
            session.with_suffix(".mcp.json").unlink(missing_ok=True)  # 可能含解析后的 MCP 密钥
            if board:
                board.close()
                (self.board_dir / f"{ident}.sock").unlink(missing_ok=True)
            if outcome["status"] == "COMPLETED" and workdir:
                outcome["result"]["files"] = []  # 用户目录里的改动留在原地，不回传
            elif outcome["status"] == "COMPLETED" and not job.get("artifact_transport"):
                # Standalone jobs and older Kernels only accept the bounded inline file result.
                outcome["result"]["files"] = self.collect_files(workspace)
            elif outcome["status"] == "COMPLETED":
                try:
                    outcome["result"]["files"] = await self.upload_files(workspace, base, attempt, stopped)
                except Exception as exc:
                    outcome = {
                        "status": "FAILED",
                        "result": {"error": f"artifact delivery failed: {type(exc).__name__}"},
                    }
            if flusher:
                # 结束前把发件箱送完，否则 finish 之后的事件会被 Kernel 拒绝。
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(outbox.join(), timeout=min(60, tolerance()))
                flusher.cancel()
                await asyncio.gather(flusher, return_exceptions=True)
            if heartbeat:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            event_log.close()
            record["finish"] = outcome
            atomic_json(ledger, record)
        # 交付失败时台账保留，由主循环 reconcile 重试。
        with contextlib.suppress(httpx.HTTPError):
            await self.finish_ledger(ledger)

    async def upload_files(self, workspace, base, attempt, stopped):
        outputs, budget, visited = [], 100 * 1024 * 1024, 0
        for directory, dirs, files in os.walk(workspace, followlinks=False):
            dirs[:] = sorted(
                d
                for d in dirs
                if not d.startswith(".") and d != "inputs" and not (Path(directory) / d).is_symlink()
            )
            for name in sorted(files):
                visited += 1
                if visited > 256 or len(outputs) >= 16:
                    return outputs
                path = Path(directory) / name
                if name.startswith(".") or path.is_symlink() or not path.is_file():
                    continue
                if not path.resolve().is_relative_to(workspace.resolve()):
                    continue
                size = path.stat().st_size
                if size > 20 * 1024 * 1024 or size > budget:
                    raise RuntimeError("output file exceeds configured transport quota")
                relative = path.relative_to(workspace).as_posix()  # 传输路径统一用 POSIX 格式
                filename = re.sub(r"[^a-zA-Z0-9._ -]", "_", name)[:150]
                filename = sha256(relative.encode()).hexdigest()[:12] + "-" + filename
                digest = sha256()
                with path.open("rb") as stream:
                    for content in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(content)
                upload = await self.post(
                    base + "/uploads",
                    {
                        "attempt_id": attempt,
                        "filename": filename,
                        "size": size,
                        "content_hash": digest.hexdigest(),
                        "media_type": "application/octet-stream",
                    },
                )
                offset = upload["received"]
                with path.open("rb") as stream:
                    stream.seek(offset)
                    while content := stream.read(upload["chunk_bytes"]):
                        if stopped.is_set():
                            raise RuntimeError("artifact upload cancelled")
                        response = await self.client.put(
                            self.prefix + base + "/uploads/" + upload["id"],
                            content=content,
                            params={"attempt_id": attempt, "offset": offset},
                        )
                        response.raise_for_status()
                        offset = response.json()["received"]
                ref = await self.post(
                    base + "/uploads/" + upload["id"] + "/finalize", {"attempt_id": attempt}
                )
                outputs.append({"path": relative, "bytes": size, "artifact": ref})
                budget -= size
        return outputs

    @staticmethod
    def collect_files(workspace):
        outputs, budget = [], 64000
        # Bound traversal as well as payload size; avoid following directory symlinks.
        candidates = []
        for directory, dirs, files in os.walk(workspace, followlinks=False):
            dirs[:] = sorted(
                d
                for d in dirs
                if not d.startswith(".")
                and not (Path(directory) / d).is_symlink()
                and not (d == "inputs" and Path(directory) == workspace)  # 依赖卡片的输入不回传
            )
            candidates.extend(Path(directory) / name for name in sorted(files) if not name.startswith("."))
            if len(candidates) >= 256:
                break
        for path in candidates[:256]:
            if len(outputs) >= 16:
                break
            if (
                path.is_symlink()
                or not path.is_file()
                or not path.resolve().is_relative_to(workspace.resolve())
            ):
                continue
            if any(part.startswith(".") for part in path.relative_to(workspace).parts):
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            relative = path.relative_to(workspace).as_posix()  # Windows 上也输出 a/b，依赖卡片才能写入
            if len(relative) > 1024:
                continue
            entry = {"path": relative, "bytes": size}
            if size <= min(16000, budget):
                try:
                    content = path.read_bytes()
                    entry["base64"] = base64.b64encode(content).decode("ascii")
                    entry["content_hash"] = sha256(content).hexdigest()
                    try:
                        entry["text"] = content.decode("utf-8")
                        entry["media_type"] = "text/plain"
                    except UnicodeError:
                        entry["media_type"] = "application/octet-stream"
                    budget -= size
                except (UnicodeError, OSError):
                    pass
            outputs.append(entry)
        if len(json.dumps(outputs).encode()) > 180000:
            for entry in outputs:
                entry.pop("text", None)
            while outputs and len(json.dumps(outputs).encode()) > 180000:
                outputs.pop()
        return outputs

    def watch_stdin(self, loop):
        """Desktop 托管时经 stdin 下发控制：drain/EOF 停止领取、当前作业结束后退出；stop 立即取消。"""

        def read():
            while True:
                command = sys.stdin.readline()
                word = command.strip().lower()
                if not command or word in {"drain", "stop"}:
                    loop.call_soon_threadsafe(self.control_command, word or "drain")
                    return

        threading.Thread(target=read, daemon=True).start()

    def control_command(self, word):
        self.draining = True
        print(f"RUNNER_{word.upper()}", flush=True)
        if word == "stop":
            for task in list(self.tasks.values()):
                task.cancel()

    async def run(self):
        async with self.client:
            self.migrate_ledger()
            if os.environ.get("NIUCAI_RUNNER_STDIN_CONTROL") == "1":
                self.watch_stdin(asyncio.get_running_loop())
            while True:
                if self.draining:
                    if not self.tasks:
                        with contextlib.suppress(httpx.HTTPError):
                            await self.reconcile()
                        print("RUNNER_EXITED", flush=True)
                        return
                    await asyncio.sleep(0.5)
                    continue
                try:
                    response = await self.post("/heartbeat", self.heartbeat_payload())
                    identity = response.get("kernel_instance_id")
                    if self.kernel_instance_id and identity != self.kernel_instance_id:
                        raise RuntimeError("KERNEL_IDENTITY_MISMATCH: refuse old ledger and new jobs")
                    if identity and not self.kernel_instance_id:
                        self.kernel_instance_id = identity
                        atomic_json(self.identity_file, {"kernel_instance_id": identity})
                    await self.reconcile()
                    # 槽位数由 Kernel 下发（旧 Kernel 不下发时按 1）；每个 Attempt 独立运行。
                    slots = max(1, int(response.get("slots") or 1))
                    if response["enabled"] and len(self.tasks) < slots:
                        job = await self.post("/claim", params={"wait": CLAIM_WAIT}, timeout=CLAIM_WAIT + 10)
                        if job:
                            print(f"Executing job {job['id']}", flush=True)
                            self.start(job)
                        continue
                except httpx.HTTPError as exc:
                    code = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                    print(
                        "TOKEN_INVALID" if code == 401 else f"KERNEL_UNREACHABLE: {type(exc).__name__}",
                        flush=True,
                    )
                except RuntimeError as exc:
                    print(str(exc), flush=True)
                await asyncio.sleep(1)


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "board-mcp":
        # 单文件 exe 内置的 niucai-board MCP 服务器（见 board_server_config）。
        import board_mcp

        board_mcp.main(sys.argv[2])
    else:
        asyncio.run(Runner().run())
