"""Deterministic acceptance provider, never a claim of real model quality."""

import asyncio
import json
import re

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

app = FastAPI()


def values(messages):
    results = []
    for message in messages:
        if message.get("role") != "tool":
            continue
        content = message.get("content", "")
        if isinstance(content, list):
            content = "\n".join(v.get("text", "") for v in content)
        try:
            results.append(json.loads(content))
        except (ValueError, TypeError):
            pass
    return results


def refs(value):
    found = {}

    def walk(v, depth=0):
        if depth > 15:
            return
        if isinstance(v, dict):
            if all(k in v for k in ("id", "version", "content_hash", "media_type")):
                found[v["id"]] = {k: v[k] for k in ("id", "version", "content_hash", "media_type")}
            for child in v.values():
                walk(child, depth + 1)
        elif isinstance(v, list):
            for child in v:
                walk(child, depth + 1)
        elif isinstance(v, str):
            try:
                walk(json.loads(v), depth + 1)
            except (ValueError, TypeError):
                # Context JSON is embedded in a system text envelope.
                for match in re.finditer(r'\{[^{}]*"content_hash"[^{}]*\}', v):
                    try:
                        walk(json.loads(match.group()), depth + 1)
                    except ValueError:
                        pass

    walk(value)
    return list(found.values())


def decide(messages, tools):
    text = json.dumps(messages, ensure_ascii=False)
    results = values(messages)
    if (
        "remote_pi_submit" in tools
        and "ANYWHERE-MAIN" not in text
        and ("ANYWHERE-DEV" in text or "ANYWHERE-VERIFY" in text)
    ):
        remote = [r for r in results if isinstance(r, dict) and r.get("parent_task_id")]
        if not remote:
            return "remote_pi_submit", {
                "prompt": "ANYWHERE-NODE-VERIFY" if "ANYWHERE-VERIFY" in text else "ANYWHERE-NODE-DEV",
                "artifacts": refs(messages),
            }
        last = remote[-1]
        if last["status"] != "COMPLETED":
            return "remote_pi_result", {"job_id": last["id"]}
        return None, "SIMULATION: delivered verified file artifacts"
    if "bot_send" in tools:
        handoffs = [r for r in results if isinstance(r, dict) and r.get("kind") == "DELEGATE"]
        replies = [r for r in results if isinstance(r, dict) and r.get("results")]
        if not handoffs:
            return "bot_send", {
                "to": "anywhere-dev",
                "content": "ANYWHERE-DEV create a binary proof",
                "output_requirements": "binary file evidence",
            }
        if len(replies) < len(handoffs):
            return "bot_wait", {"handoff_ids": [h["id"] for h in handoffs], "mode": "all"}
        if len(handoffs) == 1:
            return "bot_send", {
                "to": "anywhere-verify",
                "content": "ANYWHERE-VERIFY check the handed off binary",
                "attachments": refs(replies),
            }
        return None, "SIMULATION: main aggregated development and verification results"
    if "bash" in tools:
        if any(m.get("role") == "tool" for m in messages):
            return None, "SIMULATION: node produced evidence files"
        if "ANYWHERE-NODE-VERIFY" in text:
            artifact = refs(messages)[0]
            code = (
                "from pathlib import Path; import hashlib; "
                f"data=Path('inputs/{artifact['id']}').read_bytes(); "
                f"assert hashlib.sha256(data).hexdigest()=='{artifact['content_hash']}'; "
                "assert len(data)==131072; "
                "Path('test-report.txt').write_text('SIMULATION: binary hash verified')"
            )
        else:
            code = "from pathlib import Path; Path('proof.bin').write_bytes(bytes(range(256))*512)"
        return "bash", {"command": "python3 -c " + json.dumps(code)}
    return None, "SIMULATION: reasoning task completed"


@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    if "ANYWHERE-" not in json.dumps(body["messages"]):
        from remote_pi_model import chat as baseline_chat

        return await baseline_chat(request)
    name, result = decide(body["messages"], {t["function"]["name"] for t in body.get("tools", [])})
    call = (
        {
            "id": f"anywhere-{len(body['messages'])}",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(result)},
        }
        if name
        else None
    )
    if not body.get("stream"):
        return {
            "id": "deterministic",
            "model": body["model"],
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None if call else result,
                        **({"tool_calls": [call]} if call else {}),
                    },
                    "finish_reason": "tool_calls" if call else "stop",
                }
            ],
            "usage": {"prompt_tokens": 20, "completion_tokens": 20},
        }

    async def stream():
        await asyncio.sleep(1)
        delta = {
            "role": "assistant",
            **({"tool_calls": [{"index": 0, **call}]} if call else {"content": result}),
        }
        for value in [
            {"delta": delta, "finish_reason": None},
            {"delta": {}, "finish_reason": "tool_calls" if call else "stop"},
        ]:
            yield (
                "data: "
                + json.dumps(
                    {"id": "deterministic", "model": body["model"], "choices": [{"index": 0, **value}]}
                )
                + "\n\n"
            )
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")
