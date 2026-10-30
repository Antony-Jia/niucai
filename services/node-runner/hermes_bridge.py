"""Hermes bridge: drives one Hermes `/v1/runs` run and reports it as JSON lines on stdout.

Runner starts it as the Hermes engine's "process", so the usual process-group kill, timeout and ledger handling
apply unchanged. Frames (one JSON object per line): started, message, tool_start, tool_end, done, error.
Usage: hermes_bridge.py <run-file> <attempt-id> [<resume-session-id>]; the prompt arrives on stdin and
NIUCAI_HERMES_URL / NIUCAI_HERMES_API_KEY in the environment. The run id is written to <run-file> right after
the run starts, so Runner can ask Hermes to stop it even when this process is killed.
"""

import json
import os
import sys
import time
from pathlib import Path

import httpx

TERMINAL = {"completed", "failed", "cancelled", "interrupted"}
FAILURE_EVENTS = {"run.failed", "run.cancelled", "run.interrupted"}
POLL_SECONDS = 2
POLL_ERRORS = 5  # consecutive poll failures before the run is reported lost


def emit(frame):
    print(json.dumps(frame, ensure_ascii=False), flush=True)


def delta_text(data):
    """Text of a `message.delta` payload; the field name is not documented, so accept the usual ones."""
    for key in ("delta", "text", "content"):
        if isinstance(data.get(key), str):
            return data[key]
    return ""


def events(response):
    """(name, payload) pairs of a Server-Sent Events response; comment lines (keepalives) are skipped."""
    name, lines = "", []
    for line in response.iter_lines():
        if line.startswith(":"):
            continue
        if not line:
            if lines:
                text = "\n".join(lines)
                try:
                    data = json.loads(text)
                except ValueError:
                    data = {"text": text}
                if not isinstance(data, dict):
                    data = {"value": data}
                yield name or str(data.get("event") or data.get("type") or ""), data
            name, lines = "", []
        elif line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:"):
            lines.append(line[5:].lstrip())


def watch(client, run_id, state):
    """Follow the SSE stream until a terminal event; return the terminal payload, or None if the stream ends."""
    with client.stream("GET", f"/v1/runs/{run_id}/events") as response:
        response.raise_for_status()
        for name, data in events(response):
            if name == "message.delta":
                state["streamed"] += delta_text(data)
            elif name == "message.interim" and isinstance(data.get("text"), str) and data["text"].strip():
                emit({"type": "message", "text": data["text"]})
            elif name == "tool.started":
                emit(
                    {
                        "type": "tool_start",
                        "tool": str(data.get("tool", "tool")),
                        "preview": data.get("preview"),
                    }
                )
            elif name == "tool.completed":
                emit(
                    {
                        "type": "tool_end",
                        "tool": str(data.get("tool", "tool")),
                        "error": bool(data.get("error")),
                        "preview": data.get("preview"),
                    }
                )
            elif name == "approval.request":
                # Unattended runs cannot be approved from here; stopping beats parking until the job times out.
                return {"event": "approval.request"}
            elif name == "run.completed" or name in FAILURE_EVENTS:
                return {"event": name, **data}
    return None


def poll(client, run_id):
    """Fetch the run until it reaches a terminal (or approval-blocked) status."""
    failures = 0
    while True:
        try:
            response = client.get(f"/v1/runs/{run_id}")
            response.raise_for_status()
            run = response.json()
            failures = 0
        except (httpx.HTTPError, ValueError):
            failures += 1
            if failures >= POLL_ERRORS:
                raise
            time.sleep(POLL_SECONDS)
            continue
        status = str(run.get("status", ""))
        if status in TERMINAL or status == "waiting_for_approval":
            return run
        time.sleep(POLL_SECONDS)


def usage_of(run, event):
    usage = run.get("usage") or event.get("usage") or {}
    return {k: v for k, v in usage.items() if isinstance(v, int) and not isinstance(v, bool)}


def main(argv=None, stdin=None):
    run_file, attempt, *rest = argv if argv is not None else sys.argv[1:]
    prompt = (stdin or sys.stdin).read()
    url = os.environ["NIUCAI_HERMES_URL"].rstrip("/")
    headers = {"Authorization": f"Bearer {os.environ.get('NIUCAI_HERMES_API_KEY', '')}"}
    # Always name the session: Hermes echoes it back, which is what makes "continue this session" reliable.
    session = (rest[0] if rest and rest[0] else f"niucai-{attempt}")[:200]
    body = {"input": prompt, "session_id": session}
    # Keepalives arrive every 10 s, so a silent stream for a minute means the connection is dead.
    timeout = httpx.Timeout(30, read=60)
    state = {"streamed": ""}
    run_id = ""
    try:
        with httpx.Client(base_url=url, headers=headers, timeout=timeout) as client:
            started = client.post(
                "/v1/runs", json=body, headers={"Idempotency-Key": f"niucai-{attempt}"[:255]}
            )
            started.raise_for_status()
            run_id = str(started.json()["run_id"])
            Path(run_file).write_text(run_id, encoding="utf-8")
            emit({"type": "started", "run_id": run_id})
            terminal = None
            try:
                terminal = watch(client, run_id, state)
            except httpx.HTTPError:
                pass  # the stream is only a progress feed; the final state comes from the run record
            if terminal and terminal["event"] == "approval.request":
                client.post(f"/v1/runs/{run_id}/stop")
                raise RuntimeError(
                    "Hermes asked for approval, which an unattended job cannot give; "
                    "relax the approval policy of the Hermes profile used by niucai"
                )
            run = poll(client, run_id)
            status = str(run.get("status", ""))
            if status == "waiting_for_approval":
                client.post(f"/v1/runs/{run_id}/stop")
                raise RuntimeError("Hermes is waiting for an approval that an unattended job cannot give")
            if status != "completed":
                detail = run.get("error") or (terminal or {}).get("error") or run.get("output") or ""
                raise RuntimeError(f"Hermes run {status}: {detail}"[:500])
            answer = run.get("output") or (terminal or {}).get("output") or state["streamed"]
            emit(
                {
                    "type": "done",
                    "answer": str(answer or ""),
                    "run_id": run_id,
                    "session_id": str(run.get("session_id") or session),
                    "usage": usage_of(run, terminal or {}),
                }
            )
    except httpx.HTTPStatusError as exc:
        emit({"type": "error", "message": f"Hermes API returned HTTP {exc.response.status_code}"})
    except (httpx.HTTPError, RuntimeError, KeyError, ValueError) as exc:
        emit({"type": "error", "message": f"{exc}" if isinstance(exc, RuntimeError) else type(exc).__name__})


if __name__ == "__main__":
    main()
