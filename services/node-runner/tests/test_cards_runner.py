"""P2 白板相关的 Runner 行为：卡片工作区、内联输入、Bot 模型、board MCP 与多台账。

Run from services/kernel: `uv run --no-sync pytest ../node-runner/tests -q`.
"""

import asyncio
import base64
import importlib.util
import io
import json
import sys
import types
from pathlib import Path

import httpx
import pytest

if "fcntl" not in sys.modules:  # runner.py is Linux-only; unit tests also run on Windows
    sys.modules["fcntl"] = types.ModuleType("fcntl")


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parents[1] / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner = load("node_runner_cards", "runner.py")
runner.WINDOWS = False  # 这些用例覆盖 Linux 分支；Windows 分支见 test_windows_runner.py
board_mcp = load("board_mcp", "board_mcp.py")
CARD = "0f8e2f8c-1111-2222-3333-444455556666"


def card_job(**snapshot):
    return {
        "id": "job-1",
        "attempt_id": "a1",
        "card_id": CARD,
        "prompt": "# 卡片：feature",
        "board_token": "board-secret",
        "config_snapshot": {"workspace": f"card-{CARD}", "bot": {"name": "coder", "role": "dev"}, **snapshot},
    }


@pytest.fixture
def claude(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.os, "chown", lambda *a: None, raising=False)
    monkeypatch.setenv("NIUCAI_CLAUDE_API_KEY", "sk-claude")
    monkeypatch.setenv("NIUCAI_CLAUDE_MODEL", "claude-default")
    monkeypatch.setenv("NIUCAI_CLAUDE_PROFILES", "read,workspace,full")
    monkeypatch.setattr(runner.shutil, "which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(runner.ClaudeEngine, "probe_version", staticmethod(lambda: "2.1.307"))
    return runner.ClaudeEngine(tmp_path)


@pytest.fixture
def instance(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.os, "chown", lambda *a: None, raising=False)
    value = object.__new__(runner.Runner)
    value.root, value.control = tmp_path, tmp_path / "control"
    value.work, value.draining = tmp_path / "workspaces", False
    value.ledgers = value.control / "active"
    value.ledgers.mkdir(parents=True)
    value.board_dir = tmp_path / "board"
    value.tasks, value.prefix = {}, "/api/remote/runner/n1"
    value.job_workspaces, value.job_workdirs, value.job_uids = {}, {}, {}
    return value


def test_claude_board_mcp_and_bot_model(claude, tmp_path):
    job = card_job(bot={"name": "coder", "model": "claude-bot-model"})
    args = claude.command(tmp_path / "s.jsonl", job, tmp_path / "board" / "job-1.sock")
    config = json.loads(args[args.index("--mcp-config") + 1])
    server = config["mcpServers"]["niucai-board"]
    assert server["args"][0].endswith("board_mcp.py") and server["args"][1].endswith("job-1.sock")
    assert "board-secret" not in json.dumps(args)  # 板令牌只在 Runner 内
    allowed = args[args.index("--allowedTools") : args.index("--allowedTools") + 6]
    assert "mcp__niucai-board" in allowed
    assert args[args.index("--model") + 1] == "claude-bot-model"
    read = claude.command(tmp_path / "s.jsonl", card_job(permission_profile="read"), "x.sock")
    assert read[read.index("--allowedTools") + 1] == "mcp__niucai-board"
    plain = claude.command(tmp_path / "s.jsonl", {})
    assert json.loads(plain[plain.index("--mcp-config") + 1]) == {"mcpServers": {}}
    assert plain[plain.index("--model") + 1] == "claude-default"
    with pytest.raises(ValueError):
        claude.command(tmp_path / "s.jsonl", card_job(bot={"model": "x; rm -rf"}))


def test_codex_board_mcp_config(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.os, "chown", lambda *a: None, raising=False)
    monkeypatch.setenv("NIUCAI_CODEX_API_KEY", "sk-codex")
    monkeypatch.setattr(runner.shutil, "which", lambda name: "/usr/bin/codex")
    monkeypatch.setattr(runner.CodexEngine, "probe_version", staticmethod(lambda: "0.164.0"))
    monkeypatch.setattr(runner.CodexEngine, "probe_sandbox", lambda self: "engine")
    engine = runner.CodexEngine(tmp_path)
    args = engine.command(tmp_path / "s.jsonl", card_job(bot={"model": "gpt-bot"}), "/data/board/job-1.sock")
    assert args[args.index("-m") + 1] == "gpt-bot"
    command = next(a for a in args if a.startswith("mcp_servers.niucai-board.args="))
    assert json.loads(command.split("=", 1)[1])[1] == "/data/board/job-1.sock"
    # bot_ask 最长挂起 120 秒：Codex 的 MCP 工具超时（默认 60 秒）必须调大，否则会把它截断。
    assert f"mcp_servers.niucai-board.tool_timeout_sec={runner.ASK_TOOL_TIMEOUT}" in args
    assert args[-1] == "-"


def test_card_inputs_are_written_inside_inputs_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.os, "chown", lambda *a: None, raising=False)
    files = [{"path": "docs/spec.md", "base64": base64.b64encode(b"# spec").decode()}]
    runner.write_card_inputs(tmp_path, [{"card_id": CARD, "files": files}], 10003)
    assert (tmp_path / "inputs" / CARD / "docs" / "spec.md").read_bytes() == b"# spec"
    for bad in ("../escape", "/abs", "a/../../b", "c:\\x", "a\\b", ""):
        with pytest.raises(RuntimeError):
            runner.write_card_inputs(tmp_path, [{"card_id": CARD, "files": [{"path": bad, "base64": ""}]}], 1)
    with pytest.raises(RuntimeError):
        runner.write_card_inputs(tmp_path, [{"card_id": "../x", "files": []}], 1)
    # 回传的文件不包含 inputs/ 下的依赖输入。
    (tmp_path / "out.txt").write_text("result")
    assert [f["path"] for f in runner.Runner.collect_files(tmp_path)] == ["out.txt"]


def test_card_workspace_and_prompt(instance):
    job = card_job(instructions="只改 src/")
    assert instance.workspace_for(job).name == f"card-{CARD}"
    with pytest.raises(RuntimeError):
        instance.workspace_for(card_job(workspace="../etc"))
    # 工作区只是缓存：续接时它已丢失，不报错，而是丢掉 resume 从头来过（历史在 Kernel 里）。
    lost = card_job(workspace="bg-bot1-goal1", resume={"job_id": "old", "session_id": "s1"})
    assert instance.workspace_for(lost).name == "bg-bot1-goal1"
    assert "resume" not in lost["config_snapshot"]
    prompt = runner.Runner.compose_prompt(card_job(instructions="只改 src/", card_inputs=[{"files": "x"}]))
    assert prompt.startswith("你是 Bot @coder，角色：dev") and "只改 src/" in prompt
    assert "card_inputs" not in prompt and "# 卡片：feature" in prompt


def test_legacy_ledger_migrates_and_reconcile_skips_running(instance):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(409 if "gone" in request.url.path else 200, json={})

    instance.client = httpx.AsyncClient(base_url="http://kernel", transport=httpx.MockTransport(handler))
    (instance.control / "active.json").write_text(json.dumps({"id": "old-1", "attempt_id": "a"}))
    instance.migrate_ledger()
    assert not (instance.control / "active.json").exists()
    (instance.ledgers / "gone-1.json").write_text(json.dumps({"id": "gone-1", "attempt_id": "b"}))
    (instance.ledgers / "live-1.json").write_text(json.dumps({"id": "live-1", "attempt_id": "c"}))
    instance.tasks = {"live-1": object()}
    asyncio.run(instance.reconcile())
    prefix = "/api/remote/runner/n1/jobs/"
    assert sorted(calls) == [prefix + "gone-1/finish", prefix + "old-1/finish"]
    assert [p.name for p in instance.ledgers.iterdir()] == ["live-1.json"]


def test_board_call_forwards_token_and_reports_errors(instance):
    seen = []

    def handler(request):
        seen.append((request.url.path, request.headers.get("x-board-token"), json.loads(request.content)))
        if request.url.path.endswith("card_submit"):
            return httpx.Response(409, json={"detail": "Attempt 已结束，白板只读"})
        return httpx.Response(200, json={"ok": True})

    instance.client = httpx.AsyncClient(base_url="http://kernel", transport=httpx.MockTransport(handler))
    job = card_job()
    assert asyncio.run(instance.board_call(job, "card_progress", {"text": "hi"})) == {
        "ok": True,
        "result": {"ok": True},
    }
    path = "/api/remote/runner/n1/jobs/job-1/board/card_progress"
    assert seen[0] == (path, "board-secret", {"args": {"text": "hi"}})
    assert asyncio.run(instance.board_call(job, "card_submit", {}))["error"] == "Attempt 已结束，白板只读"
    assert asyncio.run(instance.board_call(job, "../x", {}))["ok"] is False


def test_board_mcp_protocol(monkeypatch):
    calls = []

    def fake_call(path, tool, args):
        calls.append((tool, args))
        return {"ok": True, "result": {"n": 1}}

    monkeypatch.setattr(board_mcp, "call", fake_call)
    progress = {"name": "card_progress", "arguments": {"text": "a"}}
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": progress},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "shell", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 5, "method": "resources/list"},
    ]
    output = io.StringIO()
    board_mcp.main("/tmp/x.sock", io.StringIO("\n".join(json.dumps(x) for x in lines) + "\n"), output)
    replies = [json.loads(x) for x in output.getvalue().splitlines()]
    assert [r["id"] for r in replies] == [1, 2, 3, 4, 5]
    assert replies[0]["result"]["protocolVersion"] == "2025-03-26"
    assert {t["name"] for t in replies[1]["result"]["tools"]} == board_mcp.NAMES
    assert replies[2]["result"]["isError"] is False and calls[0][0] == "card_progress"
    assert calls[0][1]["idempotency_key"]  # 写操作自动带幂等键
    assert replies[3]["result"]["isError"] is True and len(calls) == 1
    assert replies[4]["error"]["code"] == -32601


STDIO = {
    "name": "fs",
    "transport": "stdio",
    "config": {"command": "npx", "args": ["fs"], "env": {"TOKEN": "env:FS_TOKEN"}},
}
HTTP = {
    "name": "web",
    "transport": "http",
    "config": {"url": "https://mcp.example/x", "headers": {"X-Key": "lit"}},
}


def test_job_mcp_servers_resolve_node_env_and_skip_invalid(monkeypatch):
    monkeypatch.delenv("FS_TOKEN", raising=False)
    bad = [{"name": "niucai-board", "transport": "stdio", "config": {"command": "x"}}]
    bad += [{"name": "bad url", "transport": "http", "config": {"url": "https://x"}}]
    bad += [{"name": "h", "transport": "http", "config": {"url": "file:///etc"}}]
    servers, skipped = runner.job_mcp_servers(card_job(mcp_servers=[STDIO, HTTP, *bad]))
    assert servers == {"web": {"url": "https://mcp.example/x", "headers": {"X-Key": "lit"}}}
    assert skipped[0] == "fs：节点缺少环境变量 FS_TOKEN" and len(skipped) == 4
    monkeypatch.setenv("FS_TOKEN", "node-secret")
    servers, skipped = runner.job_mcp_servers(card_job(mcp_servers=[STDIO]))
    assert servers["fs"]["env"] == {"TOKEN": "node-secret"} and skipped == []
    # 只读档位不加载外部 MCP。
    servers, skipped = runner.job_mcp_servers(card_job(permission_profile="read", mcp_servers=[STDIO]))
    assert servers == {} and "只读" in skipped[0]


def test_claude_and_codex_receive_bot_mcp_servers(claude, tmp_path, monkeypatch):
    monkeypatch.setenv("FS_TOKEN", "node-secret")
    servers, _ = runner.job_mcp_servers(card_job(mcp_servers=[STDIO, HTTP]))
    args = claude.command(tmp_path / "s.jsonl", card_job(), "x.sock", mcp=servers)
    assert "node-secret" not in json.dumps(args)  # 密钥写入 0600 文件，不进命令行
    config = json.loads(Path(args[args.index("--mcp-config") + 1]).read_text(encoding="utf-8"))
    assert config["mcpServers"]["fs"]["env"] == {"TOKEN": "node-secret"}
    assert config["mcpServers"]["web"] == {
        "type": "http",
        "url": "https://mcp.example/x",
        "headers": {"X-Key": "lit"},
    }
    assert {"mcp__niucai-board", "mcp__fs", "mcp__web"} <= set(args)

    monkeypatch.setenv("NIUCAI_CODEX_API_KEY", "sk-codex")
    monkeypatch.setattr(runner.shutil, "which", lambda name: "/usr/bin/codex")
    monkeypatch.setattr(runner.CodexEngine, "probe_version", staticmethod(lambda: "0.164.0"))
    monkeypatch.setattr(runner.CodexEngine, "probe_sandbox", lambda self: "engine")
    codex = runner.CodexEngine(tmp_path).command(tmp_path / "c.jsonl", card_job(), None, mcp=servers)
    assert 'mcp_servers.fs.command="npx"' in codex and 'mcp_servers.fs.env.TOKEN="node-secret"' in codex
    assert 'mcp_servers.web.url="https://mcp.example/x"' in codex
    assert 'mcp_servers.web.http_headers."X-Key"="lit"' in codex


def test_skills_are_written_and_listed_in_prompt(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.os, "chown", lambda *a: None, raising=False)
    skills = [
        {"name": "review", "description": "代码\n审查", "content": "先读 diff"},
        {"name": "raw", "content": "---\nname: raw\ndescription: x\n---\nbody"},
        {"name": "../escape", "content": "x"},
    ]
    written = runner.write_skills(tmp_path / "assets", skills, 10003)
    assert [name for name, *_ in written] == ["review", "raw"]
    review = (tmp_path / "assets" / ".claude" / "skills" / "review" / "SKILL.md").read_text(encoding="utf-8")
    assert review.startswith('---\nname: review\ndescription: "代码 审查"\n---') and review.endswith(
        "先读 diff"
    )
    assert (
        (tmp_path / "assets" / ".claude" / "skills" / "raw" / "SKILL.md")
        .read_text(encoding="utf-8")
        .endswith("body")
    )
    prompt = runner.Runner.compose_prompt(card_job(), None, written, ["fs：节点缺少环境变量 FS_TOKEN"])
    assert "- review：代码 审查 —— " in prompt and str(written[0][2]) in prompt
    assert "以下 MCP 服务器本次未加载：fs" in prompt
    legacy = runner.Runner.compose_prompt(
        {"prompt": "go", "config_snapshot": {"skills": skills}}, None, written
    )
    assert "先读 diff" not in legacy and "review" in legacy


def test_unacked_events_replay_before_finish(instance):
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append((request.url.path.rsplit("/", 1)[-1], body.get("sequence")))
        return httpx.Response(200, json={"accepted": True})

    instance.client = httpx.AsyncClient(base_url="http://kernel", transport=httpx.MockTransport(handler))
    ledger = instance.ledgers / "job-1.json"
    finish = {"status": "COMPLETED", "result": {"answer": "ok"}}
    ledger.write_text(json.dumps({"id": "job-1", "attempt_id": "a1", "acked": 1, "finish": finish}))
    events = [{"attempt_id": "a1", "sequence": n, "kind": "log", "data": {}} for n in (1, 2, 3)]
    events.insert(1, {"attempt_id": "old", "sequence": 9, "kind": "log", "data": {}})
    lines = [json.dumps(e) for e in events] + ['{"attempt_id": "a1", "seq']  # 崩溃时的半行
    ledger.with_suffix(".events.jsonl").write_text("\n".join(lines), encoding="utf-8")
    asyncio.run(instance.reconcile())
    # 已确认的 1 号与其它 Attempt 的事件不补交；补齐后才交结论，台账与事件日志一并清理。
    assert calls == [("events", 2), ("events", 3), ("finish", None)]
    assert list(instance.ledgers.iterdir()) == []


def test_replay_stops_when_kernel_rejects_events(instance):
    calls = []

    def handler(request):
        calls.append(request.url.path.rsplit("/", 1)[-1])
        if request.url.path.endswith("/events"):
            return httpx.Response(409, json={"detail": "job is not running"})
        return httpx.Response(409, json={})

    instance.client = httpx.AsyncClient(base_url="http://kernel", transport=httpx.MockTransport(handler))
    ledger = instance.ledgers / "job-1.json"
    ledger.write_text(json.dumps({"id": "job-1", "attempt_id": "a1"}))
    lines = [json.dumps({"attempt_id": "a1", "sequence": n, "kind": "log", "data": {}}) for n in (1, 2)]
    ledger.with_suffix(".events.jsonl").write_text("\n".join(lines), encoding="utf-8")
    asyncio.run(instance.reconcile())
    assert calls == ["events", "finish"] and list(instance.ledgers.iterdir()) == []


def test_transport_paths_are_posix_and_card_timeout(tmp_path, monkeypatch):
    (tmp_path / "src" / "pages").mkdir(parents=True)
    (tmp_path / "src" / "pages" / "login.tsx").write_text("x")
    assert [f["path"] for f in runner.Runner.collect_files(tmp_path)] == ["src/pages/login.tsx"]
    monkeypatch.setenv("NIUCAI_PI_JOB_TIMEOUT", "1800")
    assert runner.job_timeout({"timeout_seconds": 600}) == 600
    for bad in (None, 0, -5, 90000, "600", True):
        assert runner.job_timeout({"timeout_seconds": bad}) == 1800


def test_inputs_dir_is_rewritten_and_changed_files_only(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.os, "chown", lambda *a: None, raising=False)
    first = [{"path": "a.txt", "base64": base64.b64encode(b"a").decode()}]
    runner.write_card_inputs(tmp_path, [{"card_id": CARD, "files": first}], None)
    (tmp_path / "inputs" / "fetched").mkdir()
    (tmp_path / "inputs" / "fetched" / "keep.txt").write_text("k")
    other = "1f8e2f8c-1111-2222-3333-444455556666"
    runner.write_card_inputs(tmp_path, [{"card_id": other, "files": first}], None)
    assert not (tmp_path / "inputs" / CARD).exists()  # 共用工作区里上一张卡的输入被清掉
    assert (tmp_path / "inputs" / other / "a.txt").exists()
    assert (tmp_path / "inputs" / "fetched" / "keep.txt").exists()
    # 同一工作区里更早的卡片留下的文件不再回传，只回传本次改动。
    (tmp_path / "old.txt").write_text("old")
    before = runner.Runner.file_stamps(tmp_path)
    (tmp_path / "new.txt").write_text("new")
    (tmp_path / "old.txt").write_text("changed!")
    assert [f["path"] for f in runner.Runner.collect_files(tmp_path, before)] == ["new.txt", "old.txt"]
    assert [f["path"] for f in runner.Runner.collect_files(tmp_path, runner.Runner.file_stamps(tmp_path))] == []


def test_environment_probe_reports_facts_only(tmp_path, monkeypatch):
    repo = tmp_path / "proj"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setenv("NIUCAI_WORKDIR_ALLOW", str(tmp_path))
    monkeypatch.setenv("NIUCAI_BROWSER_CDP_URL", "http://127.0.0.1:9222")
    monkeypatch.setattr(runner, "git_remote", lambda directory: "https://example.com/team/proj.git")
    monkeypatch.setattr(runner.shutil, "which", lambda name: "/bin/x" if name in {"git", "node"} else None)
    environment = runner.probe_environment({"claude": {"available": True, "version": "2.1.307"}})
    assert environment["engines"] == {"claude": {"available": True, "version": "2.1.307"}}
    assert environment["browser"] == {"available": True, "cdp": True}
    assert environment["repos"] == [{"name": "proj", "remote": "https://example.com/team/proj.git"}]
    assert environment["cli"] == ["git", "node"] and environment["os"]
    assert "127.0.0.1" not in json.dumps(environment)  # CDP 地址本身不上报


def test_environment_probe_is_cached_between_heartbeats(instance, monkeypatch):
    runs = []
    monkeypatch.setattr(runner, "probe_environment", lambda executors: runs.append(1) or {"os": "x"})
    assert instance.environment_probe({}) == {"os": "x"}
    instance.environment_probe({})
    assert len(runs) == 1


def test_artifact_put_reads_workspace_file_and_get_writes_inputs(instance, tmp_path):
    workspace = tmp_path / "workspaces" / "bg-b-g"
    workspace.mkdir(parents=True)
    (workspace / "report.md").write_text("# r")
    (workspace / "big.bin").write_bytes(b"x" * (runner.ARTIFACT_MAX + 1))
    outside = tmp_path / "secret.txt"
    outside.write_text("s")
    sent = []

    def handler(request):
        body = json.loads(request.content)["args"]
        sent.append((request.url.path.rsplit("/", 1)[1], body))
        if request.url.path.endswith("artifact_get"):
            return httpx.Response(
                200, json={"artifact_id": "abcd1234-0", "name": "r.md", "size": 3, "base64": body.get("fake", "IyByCg==")}
            )
        return httpx.Response(200, json={"artifact_id": "x"})

    instance.client = httpx.AsyncClient(base_url="http://kernel", transport=httpx.MockTransport(handler))
    instance.job_workspaces["job-1"] = workspace
    job = card_job()
    reply = asyncio.run(instance.board_call(job, "artifact_put", {"path": "report.md"}))
    assert reply["ok"] and sent[0][1] == {"name": "report.md", "base64": base64.b64encode(b"# r").decode()}
    for bad in ("big.bin", str(outside), "../secret.txt", "missing"):
        assert asyncio.run(instance.board_call(job, "artifact_put", {"path": bad}))["ok"] is False
    assert len(sent) == 1  # 被拒绝的路径没有发给 Kernel
    got = asyncio.run(instance.board_call(job, "artifact_get", {"artifact_id": "abcd1234-0"}))
    assert got["ok"] and "base64" not in got["result"]
    assert (workspace / got["result"]["path"]).read_bytes() == b"# r\n"
    assert got["result"]["path"].startswith("inputs/fetched/")
    # 文本直传不经过本地文件，原样转发。
    asyncio.run(instance.board_call(job, "artifact_put", {"name": "n.txt", "text": "hi"}))
    assert sent[-1][1] == {"name": "n.txt", "text": "hi"}


def test_artifact_put_refuses_hidden_and_secret_files(instance, tmp_path):
    workspace = tmp_path / "workspaces" / "bg-b-g"
    (workspace / ".git").mkdir(parents=True)
    (workspace / "keys").mkdir()
    secret = (".env", ".git/config", "keys/id_rsa", "server.pem", "prod.env", "credentials.json")
    for name in (*secret, "notes.md"):
        (workspace / name).write_text("x")
    instance.job_workspaces["job-1"] = workspace
    job = card_job()
    for bad in secret:
        args, failure = instance.artifact_upload_args(job, {"path": bad})
        assert failure == "hidden or secret-like files are not uploaded", bad
    assert instance.artifact_upload_args(job, {"path": "notes.md"})[1] == ""


def test_workdir_mode_keeps_fetched_artifacts_out_of_user_directory(instance, tmp_path):
    workspace = tmp_path / "workspaces" / "bg-b-g"
    workspace.mkdir(parents=True)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    (workdir / "report.md").write_text("# r")
    (workdir / ".env").write_text("TOKEN=1")

    def handler(request):
        body = {"artifact_id": "abcd1234-0", "name": "r.md", "size": 3, "base64": "IyByCg=="}
        return httpx.Response(200, json=body)

    instance.client = httpx.AsyncClient(base_url="http://kernel", transport=httpx.MockTransport(handler))
    instance.job_workspaces["job-1"], instance.job_workdirs["job-1"] = workspace, workdir
    job = card_job()
    # 相对路径按引擎 cwd（用户目录）解析；.env 不能上传。
    assert instance.artifact_upload_args(job, {"path": "report.md"})[1] == ""
    assert instance.artifact_upload_args(job, {"path": ".env"})[1]
    got = asyncio.run(instance.board_call(job, "artifact_get", {"artifact_id": "abcd1234-0"}))
    path = Path(got["result"]["path"])
    assert path.is_absolute() and path.is_relative_to(workspace / "inputs" / "fetched")
    assert path.read_bytes() == b"# r\n"
    assert sorted(p.name for p in workdir.iterdir()) == [".env", "report.md"]  # 用户目录没有多出 inputs/


def test_new_collab_tools_are_exposed_identically():
    names = [tool["name"] for tool in board_mcp.TOOLS]
    assert list(runner.BOARD_TOOLS) == names
    assert {"bots_list", "artifact_put", "artifact_get", "card_claim"} <= set(names)
    comment = next(tool for tool in board_mcp.TOOLS if tool["name"] == "card_comment")
    assert "reply_to" in comment["inputSchema"]["properties"]
    assert {"artifact_put", "card_claim"} <= board_mcp.MUTATING


def test_delegate_and_wait_tools_are_exposed_identically():
    names = [tool["name"] for tool in board_mcp.TOOLS]
    assert names[-3:] == ["card_delegate", "card_wait", "bot_ask"] == list(runner.BOARD_TOOLS[-3:])
    assert {"card_delegate", "card_wait"} <= board_mcp.MUTATING
    delegate = next(tool for tool in board_mcp.TOOLS if tool["name"] == "card_delegate")
    props = delegate["inputSchema"]["properties"]
    assert {"to", "tags", "title", "goal", "acceptance", "inputs", "claims", "wait"} <= set(props)
    assert "permission_profile" not in props  # 权限档位始终是接收方自己的
    wait = next(tool for tool in board_mcp.TOOLS if tool["name"] == "card_wait")
    assert set(wait["inputSchema"]["properties"]) == {"card_ids", "mode", "timeout"}
    source = (Path(__file__).parent.parent / "board-extension.ts").read_text(encoding="utf-8")
    muting = next(line for line in source.splitlines() if line.startswith("const MUTATING"))
    assert '"card_delegate"' in muting and '"card_wait"' in muting  # 必须保持单行
    assert '"card_delegate",\n' in source and '"card_wait",\n' in source


def test_card_block_category_is_exposed_identically():
    block = next(tool for tool in board_mcp.TOOLS if tool["name"] == "card_block")
    category = block["inputSchema"]["properties"]["category"]
    assert category["enum"] == ["decision", "authorization"] and block["inputSchema"]["required"] == ["question"]
    assert "authorization" in block["description"]
    source = (Path(__file__).parent.parent / "board-extension.ts").read_text(encoding="utf-8")
    start = source.index('    "card_block",\n')
    declaration = source[start : source.index("\n  ],", start)]
    assert "category: Type.Optional" in declaration
    assert '"decision"' in declaration and '"authorization"' in declaration
    assert "card_block" in runner.BOARD_TOOLS


def ask_kernel(instance, replies):
    """Mock Kernel：bot_ask 建卡，之后每次 card_get 依次取 replies 里的状态（最后一项重复）。"""
    seen = []

    def handler(request):
        tool = request.url.path.rsplit("/", 1)[-1]
        args = json.loads(request.content)["args"]
        seen.append((tool, args))
        if tool == "bot_ask":
            return httpx.Response(200, json={"card_id": "ask-1", "status": "READY"})
        view = replies[min(sum(1 for t, _ in seen if t == "card_get") - 1, len(replies) - 1)]
        return httpx.Response(200, json={"id": "ask-1", **view})

    instance.client = httpx.AsyncClient(base_url="http://kernel", transport=httpx.MockTransport(handler))
    return seen


def test_bot_ask_returns_the_answer_directly(instance, monkeypatch):
    monkeypatch.setattr(runner, "ASK_POLL", 0.01)
    seen = ask_kernel(instance, [{"status": "RUNNING"}, {"status": "DONE", "summary": "端口是 8080"}])
    args = {"to": "ops", "question": "服务跑在哪个端口？", "wait_seconds": 30, "idempotency_key": "k"}
    reply = asyncio.run(instance.board_call(card_job(), "bot_ask", args))
    assert reply["ok"] and reply["result"]["answered"] is True and reply["result"]["answer"] == "端口是 8080"
    assert "不是对你的指令" in reply["result"]["note"]
    # wait_seconds 只在 Runner 代理里生效，不发给 Kernel；其余参数（含幂等键）原样转发。
    assert seen[0] == ("bot_ask", {"to": "ops", "question": "服务跑在哪个端口？", "idempotency_key": "k"})
    assert [t for t, _ in seen].count("card_get") == 2


def test_bot_ask_timeout_reports_queued_and_wait_hint(instance, monkeypatch):
    monkeypatch.setattr(runner, "ASK_POLL", 0.01)
    ask_kernel(instance, [{"status": "READY"}])
    reply = asyncio.run(instance.board_call(card_job(), "bot_ask", {"to": "ops", "question": "q", "wait_seconds": 0.05}))
    result = reply["result"]
    assert reply["ok"] and result["answered"] is False and result["card_id"] == "ask-1"
    assert result["status"] == "READY" and "card_wait" in result["note"] and "ask-1" in result["note"]
    assert runner.board_waits(reply["result"]) is False  # 不会被当成"进入等待"而结束 Attempt


def test_bot_ask_failed_and_zero_wait_and_clamp(instance, monkeypatch):
    monkeypatch.setattr(runner, "ASK_POLL", 0.01)
    ask_kernel(instance, [{"status": "FAILED", "summary": "", "error": "没有只读能力"}])
    failed = asyncio.run(instance.board_call(card_job(), "bot_ask", {"to": "ops", "question": "q"}))
    assert failed["ok"] and failed["result"]["answered"] is False and "没有只读能力" in failed["result"]["note"]
    seen = ask_kernel(instance, [{"status": "DONE", "summary": "x"}])
    zero = asyncio.run(instance.board_call(card_job(), "bot_ask", {"to": "ops", "question": "q", "wait_seconds": 0}))
    assert zero["result"]["answered"] is False and [t for t, _ in seen] == ["bot_ask"]  # 0 = 只建卡不等
    bad = asyncio.run(instance.board_call(card_job(), "bot_ask", {"to": "ops", "question": "q", "wait_seconds": "x"}))
    assert bad["ok"] is False
    assert runner.ASK_WAIT_MAX == 120 and runner.ASK_TOOL_TIMEOUT > runner.ASK_WAIT_MAX


def test_bot_ask_kernel_policy_error_is_returned(instance):
    def handler(request):
        return httpx.Response(409, json={"detail": "协作策略不允许本 Bot 提问（can_ask=false）"})

    instance.client = httpx.AsyncClient(base_url="http://kernel", transport=httpx.MockTransport(handler))
    reply = asyncio.run(instance.board_call(card_job(), "bot_ask", {"to": "ops", "question": "q"}))
    assert reply == {"ok": False, "error": "协作策略不允许本 Bot 提问（can_ask=false）"}


def test_pending_asks_hint_does_not_change_wait_judgement():
    waiting = {"next": "wait", "ready": False, "pending_asks": {"count": 2, "note": "有 2 个待回答的提问"}}
    ready = {"next": "continue", "ready": True, "pending_asks": {"count": 1, "note": "n"}}
    assert runner.board_waits({"content": [{"type": "text", "text": json.dumps(waiting)}]}) is True
    assert runner.board_waits({"content": [{"type": "text", "text": json.dumps(ready)}]}) is False
    source = (Path(__file__).parent.parent / "board-extension.ts").read_text(encoding="utf-8")
    assert '"next" in result && !(result as { ready?: boolean }).ready' in source  # Pi 扩展的判断只看 next/ready


def test_bot_ask_is_last_everywhere_with_long_timeouts():
    source = (Path(__file__).parent.parent / "board-extension.ts").read_text(encoding="utf-8")
    declared = [line.split('"')[1] for line in source.splitlines() if line.startswith('    "') and line.endswith('",')]
    assert runner.BOARD_TOOLS[-1] == "bot_ask" == board_mcp.TOOLS[-1]["name"]
    assert source.index('"bot_ask",\n    "') > source.index('"card_wait",\n    "') and "bot_ask" in declared
    muting = next(line for line in source.splitlines() if line.startswith("const MUTATING"))
    assert '"bot_ask"' in muting and "bot_ask" in board_mcp.MUTATING
    assert board_mcp.TIMEOUTS["bot_ask"] > runner.ASK_WAIT_MAX and "bot_ask: 150000" in source
    ask = board_mcp.TOOLS[-1]["inputSchema"]
    assert ask["required"] == ["to", "question"] and "wait_seconds" in ask["properties"]


def test_ask_workspace_name_and_codex_tool_timeout(instance):
    assert runner.CARD_WORKSPACE.fullmatch("ba-bot1-0f8e2f8c-1111-2222-3333-444455556666")
    answer = card_job(workspace="ba-bot1-" + CARD, purpose="answer")
    assert instance.workspace_for(answer).name == "ba-bot1-" + CARD
