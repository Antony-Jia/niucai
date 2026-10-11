"""Deterministic TEST provider. Real Pi RPC and tools; no real model intelligence."""

import asyncio
import json

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

app = FastAPI()


@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    tools = {t["function"]["name"] for t in body.get("tools", [])}
    messages = body["messages"]
    tool_results = [m for m in messages if m.get("role") == "tool"]
    content, call = "SIMULATION: Pi task completed; inspect returned files and events.", None
    if "remote_pi_submit" in tools:
        names = [m.get("name") for m in tool_results]
        if not tool_results:
            call = {
                "id": "remote-demo-1",
                "type": "function",
                "function": {
                    "name": "remote_pi_submit",
                    "arguments": json.dumps(
                        {"prompt": "Create proof.txt containing SIMULATION remote Pi evidence"}
                    ),
                },
            }
        else:
            # Main Pi polls the child result; approvals are supplied by the demo verifier.
            last_content = tool_results[-1]["content"]
            if isinstance(last_content, list):
                last_content = "\n".join(b.get("text", "") for b in last_content)
            result = json.loads(last_content)
            if result.get("status") in {"FAILED", "CANCELLED", "DENIED", "LOST"}:
                content = "SIMULATION: remote execution did not succeed: " + result["status"]
            elif result.get("status") != "COMPLETED":
                job_id = result.get("id")
                call = {
                    "id": f"poll-{len(names)}",
                    "type": "function",
                    "function": {"name": "remote_pi_result", "arguments": json.dumps({"job_id": job_id})},
                }
            else:
                content = "SIMULATION: aggregated remote Pi result: " + result.get("result", {}).get(
                    "answer", ""
                )
    elif "write" in tools and not tool_results:
        call = {
            "id": "write-proof",
            "type": "function",
            "function": {
                "name": "write",
                "arguments": json.dumps(
                    {"path": "proof.txt", "content": "SIMULATION: real Pi CLI wrote this artifact.\n"}
                ),
            },
        }
    elif not tools:
        content = json.dumps(
            {
                "goal": "Verify remote Pi",
                "steps": [
                    {"id": "remote", "type": "reasoning", "description": "Delegate and verify remote Pi work"}
                ],
            }
        )
    if not body.get("stream"):
        message = {"role": "assistant", "content": None if call else content}
        if call:
            message["tool_calls"] = [call]
        return {
            "id": "demo",
            "model": body["model"],
            "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if call else "stop"}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 20},
        }

    async def stream():
        # Keep two runners occupied long enough to exercise queueing and cancellation.
        await asyncio.sleep(4)
        delta = (
            {"role": "assistant", "tool_calls": [{"index": 0, **call}]}
            if call
            else {"role": "assistant", "content": content}
        )
        for value in [
            {
                "id": "demo",
                "object": "chat.completion.chunk",
                "model": body["model"],
                "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
            },
            {
                "id": "demo",
                "object": "chat.completion.chunk",
                "model": body["model"],
                "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if call else "stop"}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 20},
            },
        ]:
            yield "data: " + json.dumps(value) + "\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")
