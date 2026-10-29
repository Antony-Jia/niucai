"""P4 Windows Runner 分支：命令行、环境、工作目录白名单、回环 board 代理、Job Object 与 stdin 控制。

Run from services/kernel: `uv run --no-sync pytest ../node-runner/tests -q`.
平台无关的分支通过 `runner.WINDOWS = True` 覆盖；Job Object 与 TCP 代理用例只在 Windows 上实跑。
"""

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

if "fcntl" not in sys.modules:
    sys.modules["fcntl"] = types.ModuleType("fcntl")

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))  # runner 内部 `import win_job` / `import board_mcp`


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner = load("node_runner_windows", "runner.py")
board_mcp = load("board_mcp_windows", "board_mcp.py")
windows_only = pytest.mark.skipif(os.name != "nt", reason="needs Windows")


@pytest.fixture(autouse=True)
def windows(monkeypatch):
    monkeypatch.setattr(runner, "WINDOWS", True)
    monkeypatch.setenv("NIUCAI_NODE_TOKEN", "node-secret")


@pytest.fixture
def claude(tmp_path, monkeypatch):
    monkeypatch.delenv("NIUCAI_CLAUDE_API_KEY", raising=False)
    monkeypatch.setattr(runner.shutil, "which", lambda name: r"C:\npm\claude.cmd")
    monkeypatch.setattr(runner.ClaudeEngine, "probe_version", staticmethod(lambda: "2.1.0"))
    return runner.ClaudeEngine(tmp_path)


def card_job(**snapshot):
    return {"id": "job-1", "card_id": "c1", "config_snapshot": snapshot}


def test_claude_reuses_login_and_writes_mcp_config_file(claude, tmp_path, monkeypatch):
    assert claude.uid is None
    assert claude.status()["available"] is True  # 未配置 API Key 也可用：复用已登录账号
    environment = claude.environment()
    assert "ANTHROPIC_API_KEY" not in environment and "CLAUDE_CONFIG_DIR" not in environment
    assert "NIUCAI_NODE_TOKEN" not in environment and "HOME" not in environment
    board = {"address": "tcp:127.0.0.1:5000", "secret": "s3cret"}
    args = claude.command(tmp_path / "job-1.jsonl", card_job(), board, extra_dirs=[tmp_path / "inputs"])
    assert "--bare" not in args
    config = json.loads(Path(args[args.index("--mcp-config") + 1]).read_text(encoding="utf-8"))
    server = config["mcpServers"]["niucai-board"]
    assert server["args"][-1] == "tcp:127.0.0.1:5000" and server["env"] == {"NIUCAI_BOARD_SECRET": "s3cret"}
    assert "s3cret" not in json.dumps(args)  # 密钥只在配置文件里，不上命令行
    assert args[args.index("--add-dir") + 1] == str(tmp_path / "inputs")
    monkeypatch.setenv("NIUCAI_CLAUDE_API_KEY", "sk-claude")
    assert "--bare" in claude.command(tmp_path / "job-1.jsonl", card_job())
    assert claude.environment()["ANTHROPIC_API_KEY"] == "sk-claude"


def test_codex_windows_profiles_and_board_env(tmp_path, monkeypatch):
    monkeypatch.delenv("NIUCAI_CODEX_API_KEY", raising=False)
    monkeypatch.setattr(runner.shutil, "which", lambda name: r"C:\npm\codex.cmd")
    monkeypatch.setattr(runner.CodexEngine, "probe_version", staticmethod(lambda: "0.164.0"))
    monkeypatch.setattr(runner.CodexEngine, "probe_sandbox", lambda self: "container")
    assert runner.CodexEngine(tmp_path).profiles == []  # 没有引擎沙箱、未开放 full：不接作业
    monkeypatch.setenv("NIUCAI_CODEX_PROFILES", "read,workspace,full")
    engine = runner.CodexEngine(tmp_path)
    assert engine.profiles == ["full"] and engine.status()["available"] is True
    assert "CODEX_HOME" not in engine.environment() and "CODEX_API_KEY" not in engine.environment()
    monkeypatch.setattr(runner.CodexEngine, "probe_sandbox", lambda self: "engine")
    sandboxed = runner.CodexEngine(tmp_path)
    assert sandboxed.profiles == ["read", "workspace", "full"]
    args = sandboxed.command(
        tmp_path / "s.jsonl",
        card_job(permission_profile="read"),
        {"address": "tcp:127.0.0.1:1", "secret": "k"},
    )
    assert 'mcp_servers.niucai-board.env.NIUCAI_BOARD_SECRET="k"' in args


def test_resolve_command_rejects_cmd_metacharacters(monkeypatch):
    monkeypatch.setattr(runner.shutil, "which", lambda name: r"C:\npm\claude.cmd")
    assert runner.resolve_command(["claude", "-p", "--model", "x"])[0] == r"C:\npm\claude.cmd"
    for bad in ("a&b", "a|b", "%PATH%", "a\nb", "x>y", "!v!"):
        with pytest.raises(RuntimeError):
            runner.resolve_command(["claude", bad])
    monkeypatch.setattr(runner.shutil, "which", lambda name: r"C:\tools\claude.exe")
    assert runner.resolve_command(["claude", "a&b"])[1] == "a&b"  # .exe 不经 cmd.exe
    monkeypatch.setattr(runner.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError):
        runner.resolve_command(["claude"])


def test_codex_windows_sandbox_probe_uses_direct_command(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.shutil, "which", lambda name: r"C:\npm\codex.cmd")
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(runner.subprocess, "run", run)
    engine = object.__new__(runner.CodexEngine)
    engine.home = tmp_path
    assert engine.probe_sandbox() == "engine"
    assert calls == [[r"C:\npm\codex.cmd", "sandbox", "--", "cmd", "/c", "exit", "0"]]


def test_workdir_allowlist(tmp_path, monkeypatch):
    project = tmp_path / "allowed" / "project"
    project.mkdir(parents=True)
    (tmp_path / "other").mkdir()
    monkeypatch.setenv("NIUCAI_WORKDIR_ALLOW", str(tmp_path / "allowed"))
    assert runner.allowed_workdir(str(project)) == project.resolve()
    for bad in (str(tmp_path / "other"), str(project / ".." / ".." / "other"), "relative/path"):
        with pytest.raises(RuntimeError):
            runner.allowed_workdir(bad)
    with pytest.raises(RuntimeError):
        runner.allowed_workdir(str(tmp_path / "allowed" / "missing"))
    monkeypatch.delenv("NIUCAI_WORKDIR_ALLOW")
    with pytest.raises(RuntimeError):  # 未配置白名单时一律拒绝
        runner.allowed_workdir(str(project))


def bare_runner(tmp_path):
    value = object.__new__(runner.Runner)
    value.root, value.control = tmp_path, tmp_path / "control"
    value.work, value.board_dir, value.draining = tmp_path / "work", tmp_path / "board", False
    value.tasks, value.prefix, value.kernel_instance_id = {}, "/api/remote/runner/n1", None
    return value


def test_heartbeat_without_pi_reports_windows_and_allowlist(tmp_path, monkeypatch, claude):
    monkeypatch.setenv("NIUCAI_WORKDIR_ALLOW", str(tmp_path))
    instance = bare_runner(tmp_path)
    instance.engines = {"claude": claude}
    payload = instance.heartbeat_payload()
    assert payload["platform"] == "windows" and payload["executor"] == "claude"
    assert payload["available"] is True and payload["workdir_allow"] == [str(tmp_path)]
    assert "card-board" in payload["features"] and set(payload["executors"]) == {"claude"}


def test_stop_command_cancels_tasks_and_drain_stops_claiming(tmp_path):
    instance = bare_runner(tmp_path)

    async def scenario():
        task = asyncio.create_task(asyncio.sleep(60))
        instance.tasks = {"job-1": task}
        instance.control_command("drain")
        assert instance.draining and not task.cancelled()
        instance.control_command("stop")
        await asyncio.gather(task, return_exceptions=True)
        return task.cancelled()

    assert asyncio.run(scenario()) is True


@windows_only
def test_loopback_board_proxy_requires_job_secret(tmp_path, monkeypatch):
    instance = bare_runner(tmp_path)
    seen = []

    async def board_call(job, tool, args):
        seen.append((tool, args))
        return {"ok": True, "result": {"tool": tool}}

    instance.board_call = board_call

    async def scenario():
        server, board = await instance.board_server({"id": "job-1"}, None)
        try:
            assert board["address"].startswith("tcp:127.0.0.1:") and len(board["secret"]) >= 24
            monkeypatch.setenv("NIUCAI_BOARD_SECRET", board["secret"])
            good = await asyncio.to_thread(board_mcp.call, board["address"], "board_view", {})
            monkeypatch.setenv("NIUCAI_BOARD_SECRET", "wrong")
            bad = await asyncio.to_thread(board_mcp.call, board["address"], "board_view", {})
            return good, bad
        finally:
            server.close()

    good, bad = asyncio.run(scenario())
    assert good == {"ok": True, "result": {"tool": "board_view"}}
    assert bad == {"ok": False, "error": "unauthorized"} and len(seen) == 1
    with pytest.raises(OSError):
        board_mcp.connect("tcp:10.0.0.1:80")  # 只连回环地址


@windows_only
def test_job_object_kills_process_tree():
    import win_job

    child = "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])"
    script = f"import subprocess, sys, time; {child}; time.sleep(60)"
    process = subprocess.Popen([sys.executable, "-c", script], creationflags=win_job.CREATE_NO_WINDOW)
    job = win_job.JobObject()
    try:
        job.assign(process.pid)
        assert win_job.process_start(process.pid) == runner.process_start(process.pid)
        job.terminate()
        assert process.wait(timeout=10) == 1
    finally:
        job.close()
        if process.poll() is None:
            process.kill()
    assert win_job.process_start(2**31 - 1) is None
