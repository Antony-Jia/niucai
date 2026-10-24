"""Fixture tests for Runner engines. Frames follow `codex exec --json` of the pinned CLI version.

Run from services/kernel: `uv run --no-sync pytest ../node-runner/tests -q`.
"""

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

if "fcntl" not in sys.modules:  # runner.py is Linux-only; unit tests also run on Windows
    sys.modules["fcntl"] = types.ModuleType("fcntl")

spec = importlib.util.spec_from_file_location("node_runner", Path(__file__).parents[1] / "runner.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


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
        ("tool_execution_end", {"tool": "shell", "is_error": False}),
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
    assert "secret cmd" not in json.dumps(events)  # command text is never reported


def test_codex_nonzero_exit_marks_tool_error_and_unknown_frames_are_counted(codex, tmp_path):
    frames = [
        {"type": "item.completed", "item": {"id": "a", "type": "command_execution", "exit_code": 2}},
        {"type": "item.completed", "item": {"id": "x", "type": "hologram"}},
        {"type": "surprise"},
        {"type": "item.completed", "item": {"id": "m", "type": "agent_message", "text": "ok"}},
        {"type": "turn.completed"},
    ]
    events, final = run_frames(codex, frames, tmp_path)
    assert ("tool_execution_end", {"tool": "shell", "is_error": True}) in events
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
    engines = {"pi": runner.PiEngine(tmp_path), "codex": runner.CodexEngine(tmp_path)}
    instance = object.__new__(runner.Runner)
    instance.engines, instance.requested = engines, {"pi", "codex", "claude"}
    instance.kernel_instance_id = "kernel-1"
    payload = instance.heartbeat_payload()
    assert payload["executor"] == "pi" and payload["available"] is True
    assert payload["protocol_version"] == 3 and payload["kernel_instance_id"] == "kernel-1"
    assert payload["executors"]["pi"]["available"] is True
    assert payload["executors"]["codex"] == {"available": False, "reason": "credential_missing"}
    assert payload["executors"]["claude"]["reason"] == "not_implemented"


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
