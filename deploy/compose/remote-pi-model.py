"""Deterministic TEST provider. Real Pi RPC and tools; no real model intelligence."""

import asyncio
import json

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

app = FastAPI()


def collaboration_response(messages, tools):
    """Markers are test inputs, never an autonomous production model policy."""
    text = json.dumps(messages, ensure_ascii=False)
    stages = {
        "research": ("demo-writer", "draft"),
        "draft": ("demo-reviewer", "review"),
        "review": ("demo-writer", "revision"),
        "revision": ("demo-reviewer", "approved"),
    }
    for stage in ["approved", "revision", "review", "draft", "research"]:
        if f"MULTIBOT-STAGE:{stage}" in text:
            if stage == "approved":
                return "SIMULATION: reviewer approved revised draft", None
            target, following = stages[stage]
            return "", {
                "id": f"transfer-{stage}",
                "type": "function",
                "function": {
                    "name": "bot_send",
                    "arguments": json.dumps(
                        {
                            "to": target,
                            "content": f"MULTIBOT-STAGE:{following}",
                            "kind": "TRANSFER",
                            "expects_reply": False,
                            "stage_output": f"SIMULATION stage {stage}",
                            "output_requirements": following,
                        }
                    ),
                },
            }
    if "MULTIBOT-ROOT" not in text or "bot_wait" not in tools:
        return "SIMULATION: delegated Bot result", None
    sent = []
    for message in messages:
        if message.get("role") != "tool":
            continue
        content = message.get("content", "")
        if isinstance(content, list):
            content = "\n".join(block.get("text", "") for block in content)
        try:
            value = json.loads(content)
        except (ValueError, TypeError):
            continue
        if value.get("kind") == "DELEGATE" and value.get("id"):
            sent.append(value["id"])
        if value.get("results"):
            return "SIMULATION: aggregated both Bot results", None
    if len(sent) < 2:
        name, arguments = (
            "bot_send",
            {
                "to": ["demo-writer", "demo-reviewer"][len(sent)],
                "content": "MULTIBOT-BRANCH: return evidence",
            },
        )
    else:
        name, arguments = "bot_wait", {"handoff_ids": sent, "mode": "all"}
    return "", {
        "id": f"multi-{len(sent)}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def sse(kind, data):
    return f"event: {kind}\ndata: {json.dumps({'type': kind, **data})}\n\n"


@app.post("/v1/responses")
async def responses(request: Request):
    """Minimal Responses API so the real Codex CLI can be exercised: one shell call, then an answer."""
    body = await request.json()
    finished = any(i.get("type") == "function_call_output" for i in body.get("input", []))
    if finished:
        item = {
            "type": "message",
            "role": "assistant",
            "id": "msg_sim",
            "content": [{"type": "output_text", "text": "SIMULATION: Codex wrote proof.txt"}],
        }
    else:
        item = {
            "type": "function_call",
            "id": "fc_sim",
            "call_id": "call_sim",
            "name": "exec_command",
            "arguments": json.dumps({"cmd": "echo codex-simulation > proof.txt"}),
        }
    usage = {
        "input_tokens": 3,
        "input_tokens_details": None,
        "output_tokens": 2,
        "output_tokens_details": None,
        "total_tokens": 5,
    }

    async def stream():
        yield sse("response.created", {"response": {"id": "resp_sim"}})
        yield sse("response.output_item.done", {"output_index": 0, "item": item})
        yield sse("response.completed", {"response": {"id": "resp_sim", "usage": usage}})

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.post("/v1/messages")
async def messages(request: Request):
    """Minimal Anthropic Messages API so the real Claude Code CLI can be exercised: one Bash call, then an answer."""
    body = await request.json()
    finished = any(
        isinstance(m.get("content"), list) and any(b.get("type") == "tool_result" for b in m["content"])
        for m in body.get("messages", [])
    )
    if not body.get("stream"):  # helper calls such as title generation
        return {
            "id": "msg_helper",
            "type": "message",
            "role": "assistant",
            "model": body.get("model", "sim"),
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
    if finished:
        block = {"type": "text", "text": ""}
        delta = {"type": "text_delta", "text": "SIMULATION: Claude wrote proof.txt"}
        stop = "end_turn"
    else:
        block = {"type": "tool_use", "id": "toolu_sim", "name": "Bash", "input": {}}
        command = {"command": "echo claude-simulation > proof.txt", "description": "write proof"}
        delta = {"type": "input_json_delta", "partial_json": json.dumps(command)}
        stop = "tool_use"
    start = {
        "id": "msg_sim",
        "type": "message",
        "role": "assistant",
        "model": body.get("model", "sim"),
        "content": [],
        "stop_reason": None,
        "stop_sequence": None,
        "usage": {"input_tokens": 11, "output_tokens": 1},
    }

    async def stream():
        yield sse("message_start", {"message": start})
        yield sse("content_block_start", {"index": 0, "content_block": block})
        yield sse("content_block_delta", {"index": 0, "delta": delta})
        yield sse("content_block_stop", {"index": 0})
        yield sse("message_delta", {"delta": {"stop_reason": stop, "stop_sequence": None}, "usage": {"output_tokens": 7}})
        yield sse("message_stop", {})

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    tools = {t["function"]["name"] for t in body.get("tools", [])}
    messages = body["messages"]
    tool_results = [m for m in messages if m.get("role") == "tool"]
    content, call = (
        "SIMULATION: Pi task completed; inspect returned files and events.",
        None,
    )
    if "MULTIBOT-" in json.dumps(messages):
        content, call = collaboration_response(messages, tools)
    elif "remote_pi_submit" in tools:
        names = [m.get("name") for m in tool_results]
        if not tool_results:
            call = {
                "id": "remote-demo-1",
                "type": "function",
                "function": {
                    "name": "remote_pi_submit",
                    "arguments": json.dumps(
                        {
                            "prompt": "Create proof.txt containing SIMULATION remote Pi evidence"
                        }
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
                content = (
                    "SIMULATION: remote execution did not succeed: " + result["status"]
                )
            elif result.get("status") != "COMPLETED":
                job_id = result.get("id")
                call = {
                    "id": f"poll-{len(names)}",
                    "type": "function",
                    "function": {
                        "name": "remote_pi_result",
                        "arguments": json.dumps({"job_id": job_id}),
                    },
                }
            else:
                content = "SIMULATION: aggregated remote Pi result: " + result.get(
                    "result", {}
                ).get("answer", "")
    elif "write" in tools and not tool_results:
        call = {
            "id": "write-proof",
            "type": "function",
            "function": {
                "name": "write",
                "arguments": json.dumps(
                    {
                        "path": "proof.txt",
                        "content": "SIMULATION: real Pi CLI wrote this artifact.\n",
                    }
                ),
            },
        }
    elif not tools:
        content = json.dumps(
            {
                "goal": "Verify remote Pi",
                "steps": [
                    {
                        "id": "remote",
                        "type": "reasoning",
                        "description": "Delegate and verify remote Pi work",
                    }
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
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": "tool_calls" if call else "stop",
                }
            ],
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
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "finish_reason": "tool_calls" if call else "stop",
                    }
                ],
                "usage": {"prompt_tokens": 20, "completion_tokens": 20},
            },
        ]:
            yield "data: " + json.dumps(value) + "\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")
