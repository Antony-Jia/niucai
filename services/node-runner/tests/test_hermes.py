"""Hermes engine and bridge tests; the Hermes API server is replaced by an httpx mock transport.

Run from services/kernel: `uv run --no-sync pytest ../node-runner/tests -q`.
"""

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

spec = importlib.util.spec_from_file_location("node_runner_hermes", Path(__file__).parents[1] / "runner.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
sys.path.insert(0, str(Path(__file__).parents[1]))
import hermes_bridge  # noqa: E402

runner.WINDOWS = False
REAL_CLIENT = httpx.Client  # httpx is one shared module, so patching it twice would otherwise wrap the patch


@pytest.fixture
def hermes(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.os, "chown", lambda *a: None, raising=False)
    monkeypatch.setenv("NIUCAI_HERMES_URL", "http://hermes.test:8642/")
    monkeypatch.setenv("NIUCAI_HERMES_API_KEY", "hermes-secret")
    monkeypatch.setenv("NIUCAI_NODE_TOKEN", "node-secret")
    monkeypatch.setattr(runner.HermesEngine, "probe_version", lambda self: "0.9.0")
    return runner.HermesEngine(tmp_path)


def state(tmp_path):
    return {"job_id": "job-1", "answer": "", "failed": False, "session_path": str(tmp_path / "job-1.jsonl")}


def run_frames(engine, frames, tmp_path):
    current, events, final = state(tmp_path), [], None
    for frame in frames:
        produced, done = engine.translate(frame, current)
        events += produced
        final = done or final
    return events, final


def test_status_reports_reason_without_leaking_credentials(hermes, monkeypatch):
    status = hermes.status()
    assert status["available"] and status["sandbox"] == "remote" and status["profiles"] == ["workspace"]
    assert status["models"] == [] and "hermes-secret" not in json.dumps(status)
    monkeypatch.delenv("NIUCAI_HERMES_API_KEY")
    assert hermes.status() == {"available": False, "reason": "credential_missing"}
    hermes.url = ""
    assert hermes.status() == {"available": False, "reason": "not_configured"}


def test_environment_carries_only_the_hermes_endpoint(hermes):
    env = hermes.environment()
    assert env["NIUCAI_HERMES_URL"] == "http://hermes.test:8642"
    assert env["NIUCAI_HERMES_API_KEY"] == "hermes-secret"
    assert "NIUCAI_NODE_TOKEN" not in env and "node-secret" not in json.dumps(env)


def test_command_runs_bridge_and_only_accepts_workspace(hermes, tmp_path):
    session = tmp_path / "job-1.jsonl"
    job = {"attempt_id": "att-1", "config_snapshot": {"permission_profile": "workspace"}}
    command = hermes.command(session, job)
    assert command[1].endswith("hermes_bridge.py")
    assert command[2:] == [str(tmp_path / "job-1.hermes-run"), "att-1", ""]
    resumed = {
        **job,
        "config_snapshot": {"permission_profile": "workspace", "resume": {"session_id": "sess-9"}},
    }
    assert hermes.command(session, resumed)[-1] == "sess-9"
    for profile in ("read", "full"):
        with pytest.raises(ValueError):
            hermes.command(session, {"config_snapshot": {"permission_profile": profile}})


def test_stdin_tells_hermes_to_return_results_as_text(hermes):
    text = hermes.stdin("att-1", "整理这份资料").decode()
    assert text.endswith("整理这份资料") and "最终回答" in text


def test_translate_maps_progress_and_final(hermes, tmp_path):
    events, final = run_frames(
        hermes,
        [
            {"type": "started", "run_id": "run-1"},
            {"type": "tool_start", "tool": "web_search", "preview": "query"},
            {"type": "tool_end", "tool": "web_search", "error": False, "preview": "3 results"},
            {"type": "message", "text": "thinking"},
            {
                "type": "done",
                "answer": "result",
                "run_id": "run-1",
                "session_id": "sess-1",
                "usage": {"input_tokens": 5, "x": "y"},
            },
        ],
        tmp_path,
    )
    assert [kind for kind, _ in events] == ["tool_execution_start", "tool_execution_end", "assistant"]
    assert final["answer"] == "result" and final["executor"] == "hermes"
    assert (
        final["session_id"] == "sess-1"
        and final["run_id"] == "run-1"
        and final["usage"] == {"input_tokens": 5}
    )
    assert "unknown_frames" not in final


def test_translate_done_without_streamed_message_adds_the_answer_and_counts_unknown_frames(hermes, tmp_path):
    events, final = run_frames(hermes, [{"type": "weird"}, {"type": "done", "answer": "ok"}], tmp_path)
    assert [kind for kind, _ in events] == ["assistant"]
    assert final["session_id"] == "job-1" and final["unknown_frames"] == 1


def test_translate_errors_and_empty_answers_fail(hermes, tmp_path):
    with pytest.raises(RuntimeError, match="Hermes failed: boom"):
        run_frames(hermes, [{"type": "error", "message": "boom"}], tmp_path)
    with pytest.raises(RuntimeError, match="without a final answer"):
        run_frames(hermes, [{"type": "done", "answer": ""}], tmp_path)


def test_cleanup_stops_the_remote_run_unless_it_completed(hermes, tmp_path, monkeypatch):
    stopped = []
    monkeypatch.setattr(runner.httpx, "post", lambda url, **kw: stopped.append(url))
    session = tmp_path / "job-1.jsonl"
    run_file = hermes.run_file(session)
    run_file.write_text("run-1")
    hermes.cleanup(session, stop=True)
    assert stopped == ["http://hermes.test:8642/v1/runs/run-1/stop"] and not run_file.exists()
    run_file.write_text("run-2")
    hermes.cleanup(session, stop=False)
    assert len(stopped) == 1 and not run_file.exists()
    run_file.write_text("../etc")  # a tampered run file must not become part of a URL
    hermes.cleanup(session, stop=True)
    assert len(stopped) == 1
    hermes.cleanup(session, stop=True)  # no run file: nothing to do


def sse(*items):
    body = ""
    for name, data in items:
        body += f"event: {name}\ndata: {json.dumps(data)}\n\n"
    return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})


def drive(monkeypatch, tmp_path, handler, prompt="do it", args=("att-1",), url="http://hermes.test:8642"):
    """Run the bridge against a mock Hermes; returns (frames, requests)."""
    requests = []

    def record(request):
        requests.append(request)
        return handler(request)

    monkeypatch.setattr(
        hermes_bridge.httpx, "Client", lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(record), **kw)
    )
    monkeypatch.setattr(hermes_bridge.time, "sleep", lambda s: None)
    monkeypatch.setenv("NIUCAI_HERMES_URL", url)
    monkeypatch.setenv("NIUCAI_HERMES_API_KEY", "hermes-secret")
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    run_file = tmp_path / "job.hermes-run"
    hermes_bridge.main([str(run_file), *args], stdin=io.StringIO(prompt))
    frames = [json.loads(line) for line in out.getvalue().splitlines()]
    return frames, requests, run_file


def test_bridge_streams_progress_and_takes_the_answer_from_the_run_record(monkeypatch, tmp_path):
    def handler(request):
        path = request.url.path
        if request.method == "POST" and path == "/v1/runs":
            return httpx.Response(202, json={"run_id": "run-1", "status": "started"})
        if path == "/v1/runs/run-1/events":
            return sse(
                ("tool.started", {"tool": "terminal", "preview": "ls"}),
                ("tool.completed", {"tool": "terminal", "error": False, "preview": "a b"}),
                ("message.delta", {"delta": "he"}),
                ("run.completed", {}),
            )
        if path == "/v1/runs/run-1":
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "output": "final text",
                    "session_id": "sess-1",
                    "usage": {"total_tokens": 9},
                },
            )
        raise AssertionError(path)

    frames, requests, run_file = drive(monkeypatch, tmp_path, handler, args=("att-1", "sess-0"))
    assert [f["type"] for f in frames] == ["started", "tool_start", "tool_end", "done"]
    assert frames[-1] == {
        "type": "done",
        "answer": "final text",
        "run_id": "run-1",
        "session_id": "sess-1",
        "usage": {"total_tokens": 9},
    }
    assert run_file.read_text() == "run-1"
    start = requests[0]
    assert json.loads(start.content) == {"input": "do it", "session_id": "sess-0"}
    assert (
        start.headers["Idempotency-Key"] == "niucai-att-1"
        and start.headers["Authorization"] == "Bearer hermes-secret"
    )


def test_bridge_falls_back_to_polling_and_streamed_text_when_the_stream_breaks(monkeypatch, tmp_path):
    polls = iter([{"status": "running"}, {"status": "completed", "output": ""}])

    def handler(request):
        path = request.url.path
        if request.method == "POST":
            return httpx.Response(202, json={"run_id": "run-2"})
        if path.endswith("/events"):
            return sse(("message.delta", {"delta": "streamed answer"}))  # ends without a terminal event
        return httpx.Response(200, json=next(polls))

    frames, _, _ = drive(monkeypatch, tmp_path, handler)
    assert frames[-1]["type"] == "done" and frames[-1]["answer"] == "streamed answer"


def test_bridge_stops_the_run_when_hermes_asks_for_approval(monkeypatch, tmp_path):
    def handler(request):
        path = request.url.path
        if request.method == "POST" and path == "/v1/runs":
            return httpx.Response(202, json={"run_id": "run-3"})
        if path.endswith("/events"):
            return sse(("approval.request", {"command": "rm"}))
        if path.endswith("/stop"):
            return httpx.Response(200, json={})
        raise AssertionError(path)

    frames, requests, _ = drive(monkeypatch, tmp_path, handler)
    assert frames[-1]["type"] == "error" and "approval" in frames[-1]["message"]
    assert requests[-1].url.path == "/v1/runs/run-3/stop"


def test_bridge_reports_failed_runs_and_http_errors(monkeypatch, tmp_path):
    def failing(request):
        path = request.url.path
        if request.method == "POST":
            return httpx.Response(202, json={"run_id": "run-4"})
        if path.endswith("/events"):
            return sse(("run.failed", {"error": "model unavailable"}))
        return httpx.Response(200, json={"status": "failed", "error": "model unavailable"})

    frames, _, _ = drive(monkeypatch, tmp_path, failing)
    assert frames[-1] == {"type": "error", "message": "Hermes run failed: model unavailable"}

    (tmp_path / "job.hermes-run").unlink()  # left by the first run
    frames, _, run_file = drive(monkeypatch, tmp_path, lambda request: httpx.Response(401, json={}))
    assert frames == [{"type": "error", "message": "Hermes API returned HTTP 401"}]
    assert not run_file.exists()


def test_bridge_handles_the_real_hermes_wire_format_behind_a_path_prefix(monkeypatch, tmp_path):
    """Payloads copied from a Hermes 0.22.2 API server: event names live in the data, not in `event:` lines."""
    stream = (
        ": open\n"
        'id: 0\ndata: {"event": "message.delta", "run_id": "run_x", "delta": "2", "seq": 0}\n\n'
        'id: 1\ndata: {"event": "message.delta", "run_id": "run_x", "delta": "\u3002", "seq": 1}\n\n'
        ": keepalive\n"
        'id: 2\ndata: {"event": "tool.started", "tool": "terminal", "preview": "sudo -n id", "seq": 2}\n\n'
        'id: 3\ndata: {"event": "tool.completed", "tool": "terminal", "duration": 0, "error": true, "preview": "", "seq": 3}\n\n'
        'id: 4\ndata: {"event": "run.completed", "run_id": "run_x", "output": "2\u3002", "completed": true, "seq": 4}\n\n'
        ": stream closed\n"
    )

    def handler(request):
        path = request.url.path
        if request.method == "POST":
            return httpx.Response(200, json={"run_id": "run_x", "status": "started", "replayed": False})
        if path.endswith("/events"):
            return httpx.Response(200, content=stream.encode(), headers={"content-type": "text/event-stream"})
        return httpx.Response(
            200,
            json={
                "object": "hermes.run",
                "run_id": "run_x",
                "status": "completed",
                "session_id": "niucai-att-2",
                "output": "2。",
                "usage": {"input_tokens": 10974, "output_tokens": 3, "total_tokens": 10977},
            },
        )

    frames, requests, _ = drive(
        monkeypatch, tmp_path, handler, args=("att-2",), url="https://hermes.test/niucai-api/"
    )
    assert requests[0].url.path == "/niucai-api/v1/runs"
    assert [f["type"] for f in frames] == ["started", "tool_start", "tool_end", "done"]
    assert frames[2]["error"] is True
    assert frames[-1]["answer"] == "2。" and frames[-1]["session_id"] == "niucai-att-2"
    assert frames[-1]["usage"] == {"input_tokens": 10974, "output_tokens": 3, "total_tokens": 10977}
    assert json.loads(requests[0].content)["session_id"] == "niucai-att-2"  # new runs name their session too
