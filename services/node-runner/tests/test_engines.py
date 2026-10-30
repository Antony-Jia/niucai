"""Fixture tests for Runner engines. Frames follow `codex exec --json` of the pinned CLI version.

Run from services/kernel: `uv run --no-sync pytest ../node-runner/tests -q`.
"""

import importlib.util
import json
import re
import sys
import types
from pathlib import Path

import pytest

if "fcntl" not in sys.modules:  # runner.py is Linux-only; unit tests also run on Windows
    sys.modules["fcntl"] = types.ModuleType("fcntl")

spec = importlib.util.spec_from_file_location("node_runner", Path(__file__).parents[1] / "runner.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
sys.path.insert(0, str(Path(__file__).parents[1]))
import board_mcp  # noqa: E402

runner.WINDOWS = False  # 这些用例覆盖 Linux 分支；Windows 分支见 test_windows_runner.py


@pytest.fixture
def codex(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.os, "chown", lambda *a: None, raising=False)
    monkeypatch.setenv("NIUCAI_CODEX_API_KEY", "sk-codex")
    monkeypatch.setenv("NIUCAI_CODEX_MODEL", "gpt-test")
    monkeypatch.setenv("NIUCAI_NODE_TOKEN", "node-secret")
    monkeypatch.setattr(runner.shutil, "which", lambda name: "/usr/bin/codex")
    monkeypatch.setattr(runner.CodexEngine, "probe_version", staticmethod(lambda: "0.164.0"))
    monkeypatch.setattr(runner.CodexEngine, "probe_sandbox", lambda self: "engine")
    return runner.CodexEngine(tmp_path)


def state(tmp_path):
    return {"job_id": "job-1", "answer": "", "failed": False, "session_path": str(tmp_path / "job-1.jsonl")}


def run_frames(engine, frames, tmp_path):
    current, events, final = state(tmp_path), [], None
    for frame in frames:
        produced, done = engine.translate(frame, current)
        events += produced
        final = done or final
    return events, final


def test_codex_success_maps_events_and_usage(codex, tmp_path):
    frames = [
        {"type": "thread.started", "thread_id": "th-1"},
        {"type": "turn.started"},
        {"type": "item.started", "item": {"id": "a", "type": "command_execution", "status": "in_progress"}},
        {
            "type": "item.completed",
            "item": {"id": "a", "type": "command_execution", "exit_code": 0, "command": "secret cmd"},
        },
        {"type": "item.completed", "item": {"id": "b", "type": "file_change", "status": "completed"}},
        {"type": "item.completed", "item": {"id": "r", "type": "reasoning", "text": "thinking"}},
        {"type": "item.completed", "item": {"id": "m", "type": "agent_message", "text": "done"}},
        {"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 2, "note": "x"}},
    ]
    events, final = run_frames(codex, frames, tmp_path)
    assert events == [
        ("session", {"session_id": "th-1"}),
        ("tool_execution_start", {"tool": "shell", "is_error": False}),
        ("tool_execution_end", {"tool": "shell", "is_error": False, "exit_code": 0}),
        ("tool_execution_start", {"tool": "edit", "is_error": False}),
        ("tool_execution_end", {"tool": "edit", "is_error": False}),
        ("assistant", {"text": "done", "stop_reason": "stop"}),
    ]
    assert final == {
        "answer": "done",
        "executor": "codex",
        "session_id": "th-1",
        "usage": {"input_tokens": 5, "output_tokens": 2},
    }


def test_codex_nonzero_exit_marks_tool_error_and_unknown_frames_are_counted(codex, tmp_path):
    frames = [
        {"type": "item.completed", "item": {"id": "a", "type": "command_execution", "exit_code": 2}},
        {"type": "item.completed", "item": {"id": "x", "type": "hologram"}},
        {"type": "surprise"},
        {"type": "item.completed", "item": {"id": "m", "type": "agent_message", "text": "ok"}},
        {"type": "turn.completed"},
    ]
    events, final = run_frames(codex, frames, tmp_path)
    assert ("tool_execution_end", {"tool": "shell", "is_error": True, "exit_code": 2}) in events
    assert final["unknown_frames"] == 2


def test_codex_answer_falls_back_to_last_message_file(codex, tmp_path):
    (tmp_path / "job-1.txt").write_text("from file")
    _, final = run_frames(codex, [{"type": "turn.completed"}], tmp_path)
    assert final["answer"] == "from file"
    with pytest.raises(RuntimeError, match="without a final answer"):
        run_frames(codex, [{"type": "turn.completed"}], tmp_path / "missing")


def test_codex_failures_and_transient_errors(codex, tmp_path):
    with pytest.raises(RuntimeError, match="quota"):
        run_frames(codex, [{"type": "turn.failed", "error": {"message": "quota exceeded"}}], tmp_path)
    with pytest.raises(RuntimeError, match="bad key"):
        run_frames(codex, [{"type": "error", "message": "bad key"}], tmp_path)
    events, final = run_frames(codex, [{"type": "error", "message": "Reconnecting... 1/5"}], tmp_path)
    assert events == [] and final is None


def test_codex_command_maps_profile_to_sandbox_and_rejects_others(codex, tmp_path):
    session = tmp_path / "job-1.jsonl"
    default = codex.command(session, {"config_snapshot": {}})
    assert default[:3] == ["codex", "exec", "--json"] and default[-1] == "-"
    assert default[default.index("--sandbox") + 1] == "workspace-write"
    assert default[default.index("--output-last-message") + 1] == str(tmp_path / "job-1.txt")
    assert default[default.index("-m") + 1] == "gpt-test"
    assert "project_doc_max_bytes=0" in default and 'approval_policy="never"' in default
    read = codex.command(session, {"config_snapshot": {"permission_profile": "read"}})
    assert read[read.index("--sandbox") + 1] == "read-only"
    for profile in ("full", "root"):  # `full` must be enabled explicitly on the node
        with pytest.raises(ValueError):
            codex.command(session, {"config_snapshot": {"permission_profile": profile}})


def test_codex_custom_provider_and_environment_isolation(codex, tmp_path, monkeypatch):
    codex.base_url = "http://model:8080/v1"
    args = codex.command(tmp_path / "j.jsonl", {})
    assert 'model_providers.niucai.base_url="http://model:8080/v1"' in args
    environment = codex.environment()
    assert environment["CODEX_API_KEY"] == "sk-codex"
    assert "node-secret" not in json.dumps(environment)
    assert set(environment) == {"PATH", "HOME", "CODEX_HOME", "LANG", "CODEX_API_KEY"}
    assert codex.closes_stdin and codex.stdin("a", "hello") == b"hello"


def test_codex_status_reports_reason_without_leaking_credentials(codex, monkeypatch):
    status = codex.status()
    assert status == {
        "available": True,
        "version": "0.164.0",
        "model": "gpt-test",
        "profiles": ["read", "workspace"],
        "sandbox": "engine",
    }
    assert "sk-codex" not in json.dumps(status)
    monkeypatch.delenv("NIUCAI_CODEX_API_KEY")
    assert codex.status() == {"available": False, "reason": "credential_missing"}
    monkeypatch.setattr(runner.shutil, "which", lambda name: None)
    assert codex.status() == {"available": False, "reason": "not_installed"}


def test_runner_heartbeat_is_protocol_3_with_executors(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.os, "chown", lambda *a: None, raising=False)
    monkeypatch.setenv("NIUCAI_PI_MODEL", "pi-model")
    monkeypatch.setenv("NIUCAI_CODEX_API_KEY", "")
    monkeypatch.setenv("NIUCAI_ENGINES", "pi,codex,claude")
    monkeypatch.setattr(runner.shutil, "which", lambda name: "/usr/bin/codex")
    monkeypatch.setattr(runner.CodexEngine, "probe_version", staticmethod(lambda: ""))
    monkeypatch.setattr(runner.CodexEngine, "probe_sandbox", lambda self: "engine")
    monkeypatch.setattr(runner.ClaudeEngine, "probe_version", staticmethod(lambda: "2.1.307"))
    monkeypatch.setenv("NIUCAI_CLAUDE_API_KEY", "sk-claude")
    engines = {
        "pi": runner.PiEngine(tmp_path),
        "codex": runner.CodexEngine(tmp_path),
        "claude": runner.ClaudeEngine(tmp_path),
    }
    instance = object.__new__(runner.Runner)
    instance.engines, instance.requested = engines, {"pi", "codex", "claude"}
    instance.kernel_instance_id = "kernel-1"
    payload = instance.heartbeat_payload()
    assert payload["executor"] == "pi" and payload["available"] is True
    assert payload["protocol_version"] == 3 and payload["kernel_instance_id"] == "kernel-1"
    assert payload["executors"]["pi"]["available"] is True
    assert payload["executors"]["codex"] == {"available": False, "reason": "credential_missing"}
    assert payload["executors"]["claude"]["available"] is True
    assert payload["executors"]["claude"]["sandbox"] == "container"
    assert "sk-claude" not in json.dumps(payload)


def test_pi_wire_behaviour_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.os, "chown", lambda *a: None, raising=False)
    monkeypatch.setenv("NIUCAI_PI_MODEL", "pi-model")
    pi = runner.PiEngine(tmp_path)
    assert not pi.closes_stdin
    assert pi.command(tmp_path / "s.jsonl", {})[:3] == ["pi", "--mode", "rpc"]
    current = state(tmp_path)
    pi.translate(
        {
            "type": "message_end",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
        },
        current,
    )
    _, final = pi.translate({"type": "agent_end"}, current)
    assert final["answer"] == "hi" and final["executor"] == "pi"


def test_pi_browser_is_explicit_and_has_no_shell_tools(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.os, "chown", lambda *a: None, raising=False)
    monkeypatch.setenv("NIUCAI_PI_MODEL", "pi-model")
    monkeypatch.delenv("NIUCAI_PI_BROWSER_ENABLED", raising=False)
    engine = runner.PiEngine(tmp_path)
    assert "--extension" not in engine.command(tmp_path / "s.jsonl")
    assert "NIUCAI_BROWSER_CDP_URL" not in engine.environment()
    monkeypatch.setenv("NIUCAI_PI_BROWSER_ENABLED", "true")
    args = engine.command(tmp_path / "s.jsonl", {"config_snapshot": {"permission_profile": "workspace"}})
    assert "--no-builtin-tools" in args
    assert args[args.index("--tools") + 1] == "browser"
    assert args[args.index("--extension") + 1].endswith("browser-extension.ts")
    assert engine.environment()["NIUCAI_BROWSER_CDP_URL"] == "http://127.0.0.1:9222"
    with pytest.raises(ValueError, match="Visible browser requires"):
        engine.command(tmp_path / "s.jsonl", {"config_snapshot": {"permission_profile": "read"}})


def test_pi_board_extension_loads_only_for_card_jobs(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.os, "chown", lambda *a: None, raising=False)
    monkeypatch.setenv("NIUCAI_PI_MODEL", "pi-model")
    monkeypatch.delenv("NIUCAI_PI_BROWSER_ENABLED", raising=False)
    engine = runner.PiEngine(tmp_path)
    assert "--extension" not in engine.command(tmp_path / "s.jsonl", {})
    args = engine.command(tmp_path / "s.jsonl", {}, tmp_path / "job.sock")
    assert args[args.index("--extension") + 1].endswith("board-extension.ts") and "--tools" not in args
    sock = tmp_path / "job.sock"
    assert engine.board_environment(sock) == {"NIUCAI_BOARD_ADDRESS": str(sock)}
    windows = engine.board_environment({"address": "tcp:127.0.0.1:9", "secret": "s"})
    assert windows == {"NIUCAI_BOARD_ADDRESS": "tcp:127.0.0.1:9", "NIUCAI_BOARD_SECRET": "s"}
    # 浏览器节点只放开 browser 与白板工具，不恢复 Shell。
    monkeypatch.setenv("NIUCAI_PI_BROWSER_ENABLED", "true")
    snapshot = {"config_snapshot": {"permission_profile": "workspace"}}
    tools = engine.command(tmp_path / "s.jsonl", snapshot, tmp_path / "job.sock")
    assert tools[tools.index("--tools") + 1] == ",".join(["browser", *runner.BOARD_TOOLS])
    assert "pi-board" in engine.heartbeat()["features"]


def test_pi_board_tools_match_the_mcp_server():
    source = runner.BOARD_EXTENSION.read_text(encoding="utf-8")
    declared = re.findall(r'^ {2}\[?\s*"([a-z_]+)",', source, re.M)
    assert declared == list(runner.BOARD_TOOLS) == [tool["name"] for tool in board_mcp.TOOLS]


def test_pi_submit_without_final_text_completes(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.os, "chown", lambda *a: None, raising=False)
    monkeypatch.setenv("NIUCAI_PI_MODEL", "pi-model")
    pi = runner.PiEngine(tmp_path)
    current = state(tmp_path)
    tool_only = {"role": "assistant", "content": [{"type": "toolCall"}], "stopReason": "toolUse"}
    pi.translate({"type": "message_end", "message": tool_only}, current)
    pi.translate({"type": "tool_execution_end", "toolName": "card_progress"}, current)
    with pytest.raises(RuntimeError):
        pi.translate({"type": "agent_end"}, dict(current))
    pi.translate({"type": "tool_execution_end", "toolName": "card_submit", "isError": False}, current)
    _, final = pi.translate({"type": "agent_end"}, current)
    assert "card_submit" in final["answer"]
    failed = state(tmp_path)
    pi.translate({"type": "tool_execution_end", "toolName": "card_submit", "isError": True}, failed)
    with pytest.raises(RuntimeError):
        pi.translate({"type": "agent_end"}, failed)


def test_codex_without_engine_sandbox_offers_only_container_isolated_profiles(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.os, "chown", lambda *a: None, raising=False)
    monkeypatch.setenv("NIUCAI_CODEX_API_KEY", "sk-codex")
    monkeypatch.setenv("NIUCAI_CODEX_PROFILES", "read,workspace,full")
    monkeypatch.setattr(runner.shutil, "which", lambda name: "/usr/bin/codex")
    monkeypatch.setattr(runner.CodexEngine, "probe_version", staticmethod(lambda: "0.164.0"))
    monkeypatch.setattr(runner.CodexEngine, "probe_sandbox", lambda self: "container")
    engine = runner.CodexEngine(tmp_path)
    status = engine.status()
    assert status["sandbox"] == "container" and status["profiles"] == ["workspace", "full"]
    args = engine.command(tmp_path / "j.jsonl", {})
    assert args[args.index("--sandbox") + 1] == "danger-full-access"
    with pytest.raises(ValueError):  # read-only cannot be enforced, so it is not offered
        engine.command(tmp_path / "j.jsonl", {"config_snapshot": {"permission_profile": "read"}})


@pytest.fixture
def claude(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.os, "chown", lambda *a: None, raising=False)
    monkeypatch.setenv("NIUCAI_CLAUDE_API_KEY", "sk-claude")
    monkeypatch.setenv("NIUCAI_CLAUDE_MODEL", "claude-test")
    monkeypatch.setenv("NIUCAI_NODE_TOKEN", "node-secret")
    monkeypatch.setattr(runner.shutil, "which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(runner.ClaudeEngine, "probe_version", staticmethod(lambda: "2.1.307"))
    return runner.ClaudeEngine(tmp_path)


def test_claude_success_maps_events_usage_and_cost(claude, tmp_path):
    # Frame shapes taken from `claude -p --output-format stream-json --verbose` 2.1.307.
    frames = [
        {"type": "system", "subtype": "init", "session_id": "sess-1", "tools": ["Bash"]},
        {
            "type": "assistant",
            "parent_tool_use_id": None,
            "message": {
                "content": [
                    {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "secret cmd"}}
                ]
            },
        },
        {"type": "system", "subtype": "task_summary", "detail": "writing proof"},
        {
            "type": "user",
            "message": {
                "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "x", "is_error": False}]
            },
        },
        {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "hmm"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "done"}]}},
        {"type": "system", "subtype": "post_turn_summary"},
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "all done",
            "session_id": "sess-1",
            "num_turns": 2,
            "total_cost_usd": 0.000368,
            "usage": {
                "input_tokens": 22,
                "output_tokens": 14,
                "service_tier": "standard",
                "speed": "standard",
            },
        },
    ]
    events, final = run_frames(claude, frames, tmp_path)
    assert events == [
        ("session", {"session_id": "sess-1"}),
        ("tool_execution_start", {"tool": "Bash", "is_error": False, "input": "secret cmd"}),
        ("tool_execution_end", {"tool": "Bash", "is_error": False, "output": "x"}),
        ("assistant", {"text": "done", "stop_reason": "stop"}),
    ]
    assert final == {
        "answer": "all done",
        "executor": "claude",
        "session_id": "sess-1",
        "usage": {"input_tokens": 22, "output_tokens": 14, "num_turns": 2},
        "cost_usd": 0.000368,
    }


def test_claude_tool_errors_subagents_and_unknown_frames(claude, tmp_path):
    frames = [
        {
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Edit", "input": {}}]},
        },
        {
            "type": "user",
            "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "is_error": True}]},
        },
        {
            "type": "assistant",
            "parent_tool_use_id": "agent-1",
            "message": {"content": [{"type": "text", "text": "sub-agent chatter"}]},
        },
        {"type": "assistant", "message": {"content": [{"type": "hologram"}]}},
        {"type": "surprise"},
        {"type": "rate_limit_event"},
        {"type": "result", "subtype": "success", "is_error": False, "result": "ok"},
    ]
    events, final = run_frames(claude, frames, tmp_path)
    assert ("tool_execution_end", {"tool": "Edit", "is_error": True}) in events
    assert "sub-agent chatter" not in json.dumps(events)
    assert final["unknown_frames"] == 2 and final["answer"] == "ok"


def test_claude_failed_results_raise(claude, tmp_path):
    with pytest.raises(RuntimeError, match="error_max_budget_usd"):
        run_frames(
            claude, [{"type": "result", "subtype": "error_max_budget_usd", "is_error": True}], tmp_path
        )
    with pytest.raises(RuntimeError, match="invalid key"):
        run_frames(
            claude,
            [{"type": "result", "subtype": "success", "is_error": True, "result": "invalid key"}],
            tmp_path,
        )
    with pytest.raises(RuntimeError, match="without a final answer"):
        run_frames(
            claude, [{"type": "result", "subtype": "success", "is_error": False, "result": ""}], tmp_path
        )


def test_claude_command_maps_profiles_and_isolates_configuration(claude, tmp_path):
    session = tmp_path / "job-1.jsonl"
    default = claude.command(session, {"config_snapshot": {}})
    assert default[:2] == ["claude", "-p"] and "--bare" in default and "--strict-mcp-config" in default
    assert default[default.index("--permission-mode") + 1] == "acceptEdits"
    assert default[default.index("--model") + 1] == "claude-test"
    assert default[default.index("--disallowedTools") + 1 : default.index("--disallowedTools") + 3] == [
        "WebSearch",
        "WebFetch",
    ]
    read = claude.command(session, {"config_snapshot": {"permission_profile": "read"}})
    assert read[read.index("--tools") + 1] == "Read,Glob,Grep" and "Bash" not in read
    for profile in ("full", "root"):  # `full` must be enabled explicitly on the node
        with pytest.raises(ValueError):
            claude.command(session, {"config_snapshot": {"permission_profile": profile}})


def test_claude_full_profile_budget_and_environment(claude, tmp_path, monkeypatch):
    monkeypatch.setenv("NIUCAI_CLAUDE_PROFILES", "workspace,full")
    monkeypatch.setenv("NIUCAI_CLAUDE_MAX_BUDGET_USD", "2.5")
    monkeypatch.setenv("NIUCAI_CLAUDE_BASE_URL", "http://model:8080")
    engine = runner.ClaudeEngine(tmp_path)
    assert engine.profiles == ["workspace", "full"]
    args = engine.command(tmp_path / "j.jsonl", {"config_snapshot": {"permission_profile": "full"}})
    assert args[args.index("--permission-mode") + 1] == "bypassPermissions"
    assert args[args.index("--max-budget-usd") + 1] == "2.5"
    environment = engine.environment()
    assert (
        environment["ANTHROPIC_API_KEY"] == "sk-claude"
        and environment["ANTHROPIC_BASE_URL"] == "http://model:8080"
    )
    assert "node-secret" not in json.dumps(environment) and "CODEX_API_KEY" not in environment
    assert engine.closes_stdin and engine.stdin("a", "hello") == b"hello"


def test_claude_status_reports_reason_without_leaking_credentials(claude, monkeypatch):
    status = claude.status()
    assert status == {
        "available": True,
        "version": "2.1.307",
        "model": "claude-test",
        "profiles": ["read", "workspace"],
        "sandbox": "container",
    }
    assert "sk-claude" not in json.dumps(status)
    monkeypatch.delenv("NIUCAI_CLAUDE_API_KEY")
    assert claude.status() == {"available": False, "reason": "credential_missing"}
    monkeypatch.setattr(runner.shutil, "which", lambda name: None)
    assert claude.status() == {"available": False, "reason": "not_installed"}


def test_resume_session_is_validated():
    job = {"config_snapshot": {"resume": {"job_id": "j", "session_id": "0199-abc_DEF"}}}
    assert runner.resume_session(job) == "0199-abc_DEF"
    assert runner.resume_session({"config_snapshot": {}}) is None
    for bad in ("--last", "a b", "", "x" * 101, "$(id)"):
        with pytest.raises(ValueError):
            runner.resume_session({"config_snapshot": {"resume": {"session_id": bad}}})


def test_claude_resume_adds_session_flag(claude, tmp_path):
    job = {"config_snapshot": {"resume": {"job_id": "j", "session_id": "sess-1"}}}
    args = claude.command(tmp_path / "n.jsonl", job)
    assert args[args.index("--resume") + 1] == "sess-1"
    assert "--resume" not in claude.command(tmp_path / "n.jsonl", {"config_snapshot": {}})


def test_codex_resume_uses_resume_subcommand_and_config_sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.os, "chown", lambda *a: None, raising=False)
    monkeypatch.setenv("NIUCAI_CODEX_API_KEY", "sk-codex")
    monkeypatch.setattr(runner.shutil, "which", lambda name: "/usr/bin/codex")
    monkeypatch.setattr(runner.CodexEngine, "probe_sandbox", lambda self: "engine")
    engine = runner.CodexEngine(tmp_path)
    job = {"config_snapshot": {"resume": {"job_id": "j", "session_id": "thread-1"}}}
    args = engine.command(tmp_path / "n.jsonl", job)
    assert args[:3] == ["codex", "exec", "resume"] and "--sandbox" not in args
    assert 'sandbox_mode="workspace-write"' in args
    assert args[-2:] == ["thread-1", "-"]
    fresh = engine.command(tmp_path / "n.jsonl", {"config_snapshot": {}})
    assert fresh[:2] == ["codex", "exec"] and "resume" not in fresh and fresh[-1] == "-"


def test_node_credentials_pair_once_and_persist(tmp_path, monkeypatch):
    for key in ("NIUCAI_NODE_ID", "NIUCAI_NODE_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("NIUCAI_PAIRING_CODE", "ABCD-EFGH")
    calls = []

    def post(url, json, timeout):
        calls.append((url, json))
        body = {"node_id": "n1", "token": "t1", "name": "VPS", "kernel_instance_id": "k1"}
        return types.SimpleNamespace(status_code=201, raise_for_status=lambda: None, json=lambda: body)

    monkeypatch.setattr(runner.httpx, "post", post)
    assert runner.node_credentials(tmp_path, "http://kernel") == ("n1", "t1", "k1")
    assert calls[0][0] == "http://kernel/api/pairing/redeem" and calls[0][1]["code"] == "ABCD-EFGH"
    # 第二次启动读取本地凭据，不再兑换
    assert runner.node_credentials(tmp_path, "http://kernel") == ("n1", "t1", None)
    assert len(calls) == 1


def test_node_credentials_require_pairing_code(tmp_path, monkeypatch):
    for key in ("NIUCAI_NODE_ID", "NIUCAI_NODE_TOKEN", "NIUCAI_PAIRING_CODE"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(SystemExit, match="PAIRING_REQUIRED"):
        runner.node_credentials(tmp_path, "http://kernel")


def test_codex_reports_command_output_file_changes_and_mcp_calls(codex, tmp_path):
    frames = [
        {"type": "item.started", "item": {"id": "a", "type": "command_execution", "command": "ls -la"}},
        {
            "type": "item.completed",
            "item": {
                "id": "a",
                "type": "command_execution",
                "command": "ls -la",
                "aggregated_output": "total 0",
                "exit_code": 1,
            },
        },
        {
            "type": "item.completed",
            "item": {
                "id": "b",
                "type": "file_change",
                "changes": [{"path": "a.py", "kind": "update"}, {"path": "b.py", "kind": "add"}],
            },
        },
        {
            "type": "item.completed",
            "item": {
                "id": "c",
                "type": "mcp_tool_call",
                "server": "chrome",
                "tool": "screenshot",
                "arguments": {"full": True},
                "error": {"message": "window minimized"},
                "status": "failed",
            },
        },
        {"type": "item.completed", "item": {"id": "d", "type": "web_search", "query": "niucai"}},
    ]
    events, _ = run_frames(codex, frames, tmp_path)
    assert events[0] == ("tool_execution_start", {"tool": "shell", "is_error": False, "input": "ls -la"})
    assert events[1] == (
        "tool_execution_end",
        {"tool": "shell", "is_error": True, "output": "total 0", "exit_code": 1},
    )
    assert events[2][1]["input"] == "update a.py; add b.py"
    assert events[4][1]["input"] == 'chrome.screenshot {"full": true}'
    assert events[5][1] == {"tool": "mcp", "is_error": True, "output": "window minimized"}
    assert events[6][1]["input"] == "niucai"


def test_claude_reports_tool_input_and_result_blocks(claude, tmp_path):
    frames = [
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "id": "g", "name": "Grep", "input": {"path": "src", "pattern": "TODO"}},
                    {"type": "tool_use", "id": "m", "name": "mcp__x__shot", "input": {"a": 1}},
                ]
            },
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "g",
                        "content": [{"type": "text", "text": "a.py:1"}, {"type": "image"}],
                    }
                ]
            },
        },
    ]
    events, _ = run_frames(claude, frames, tmp_path)
    assert events[0][1]["input"] == "TODO"  # pattern wins over path
    assert events[1][1]["input"] == '{"a": 1}'
    assert events[2][1] == {"tool": "Grep", "is_error": False, "output": "a.py:1\n[图片]"}


def test_pi_reports_tool_args_and_result(tmp_path, monkeypatch):
    monkeypatch.setenv("NIUCAI_PI_MODEL", "test-model")
    monkeypatch.setattr(runner.os, "chown", lambda *a: None, raising=False)
    pi = runner.PiEngine(tmp_path)
    current = state(tmp_path)
    start, _ = pi.translate({"type": "tool_execution_start", "toolName": "bash", "args": {"command": "pwd"}}, current)
    end, _ = pi.translate(
        {
            "type": "tool_execution_end",
            "toolName": "bash",
            "isError": False,
            "result": {"content": [{"type": "text", "text": "/work"}]},
        },
        current,
    )
    assert start == [("tool_execution_start", {"tool": "bash", "is_error": False, "input": "pwd"})]
    assert end == [("tool_execution_end", {"tool": "bash", "is_error": False, "output": "/work"})]


def test_event_detail_is_redacted_bounded_and_can_be_disabled(monkeypatch):
    monkeypatch.setenv("NIUCAI_CODEX_API_KEY", "sk-live-abcdef123456")
    command = (
        "curl -H 'Authorization: Bearer abcdefgh12345678' https://x "
        "&& echo sk-live-abcdef123456 GITHUB_TOKEN=ghp_abcdefghijkl password=hunter22"
    )
    detail = runner.tool_detail("shell", args={"command": command})
    for secret in ("abcdefgh12345678", "sk-live-abcdef123456", "ghp_abcdefghijkl", "hunter22"):
        assert secret not in detail["input"]
    assert "https://x" in detail["input"]
    long = runner.tool_detail("shell", output="x" * 5000 + "TAIL")
    assert len(long["output"]) <= runner.EVENT_OUTPUT_LIMIT + 1 and long["output"].endswith("TAIL")
    head = runner.tool_detail("Read", output="HEAD" + "x" * 5000)
    assert head["output"].startswith("HEAD")
    monkeypatch.setattr(runner, "EVENT_DETAIL", False)
    assert runner.tool_detail("shell", args={"command": "ls"}, output="x", exit_code=0) == {}


def test_stderr_is_drained_throttled_redacted_and_kept_as_tail(monkeypatch):
    import asyncio

    class Stream:
        def __init__(self, chunks):
            self.chunks = list(chunks)

        async def read(self, size):
            return self.chunks.pop(0) if self.chunks else b""

    reports, tail = [], []
    chunks = [b"warn: using token=abcdef123456\n", b"fatal: boom\n"]
    asyncio.run(runner.drain_stderr(Stream(chunks), reports.append, tail))
    assert reports == ["warn: using token=[已隐藏]\nfatal: boom"]  # one throttled report at EOF
    assert "fatal: boom" in tail[0]

    reports.clear()
    monkeypatch.setattr(runner, "STDERR_INTERVAL", 0)
    monkeypatch.setattr(runner, "STDERR_EVENT_MAX", 2)
    asyncio.run(runner.drain_stderr(Stream([b"a\n"] * 6), reports.append, []))
    assert len(reports) == 2  # capped per attempt
