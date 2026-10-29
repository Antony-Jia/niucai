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
    with pytest.raises(RuntimeError):  # 续接要求卡片工作区仍在
        instance.workspace_for(card_job(resume={"job_id": "old", "session_id": "s1"}))
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
