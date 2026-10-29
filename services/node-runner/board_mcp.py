"""niucai-board：给 Claude Code / Codex 用的 stdio MCP 服务器。

由引擎以自己的身份启动，参数是本作业的 Unix socket（Linux），或 `tcp:127.0.0.1:<port>`（Windows，
每作业密钥经环境变量 NIUCAI_BOARD_SECRET 传入）；每次工具调用转发给 Runner，Runner 再带节点凭据与
板令牌调用 Kernel。本进程不持有节点令牌与板令牌，只依赖标准库。
"""

import json
import os
import socket
import sys
import uuid

PROTOCOL = "2025-06-18"
ID = {"type": "string", "description": "卡片 id，省略时为当前卡片"}
TOOLS = [
    {
        "name": "board_view",
        "description": "列出同一目标下的所有卡片（id、标题、状态、负责 Bot、摘要）。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "card_get",
        "description": "读取一张卡片的目标、验收标准、状态与最近 20 条动态。",
        "inputSchema": {"type": "object", "properties": {"card_id": ID}},
    },
    {
        "name": "inbox",
        "description": "读取 @ 提及当前 Bot 的新消息（读过即标记，不会重复返回）。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "card_progress",
        "description": "在当前卡片上写一条进度，可附 0-100 的百分比。",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}, "percent": {"type": "number"}},
            "required": ["text"],
        },
    },
    {
        "name": "card_comment",
        "description": "在当前或同目标的另一张卡片上发评论，正文里用 @名称 提及其他 Bot。",
        "inputSchema": {
            "type": "object",
            "properties": {"body": {"type": "string"}, "card_id": ID},
            "required": ["body"],
        },
    },
    {
        "name": "card_propose",
        "description": "提议拆出一张新卡片，由用户或编排者决定是否创建。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "goal": {"type": "string"},
                "acceptance": {"type": "string"},
                "bot": {"type": "string", "description": "建议的负责 Bot 名称"},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["title"],
        },
    },
    {
        "name": "card_block",
        "description": "需要用户或 kernel 回答才能继续时提问；随后结束本次执行，回答后会续接会话。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "mention": {"type": "string", "enum": ["user", "kernel"]},
            },
            "required": ["question"],
        },
    },
    {
        "name": "card_submit",
        "description": "提交当前卡片的结果摘要；交付文件写在工作区内。提交后结束本次执行。",
        "inputSchema": {
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
        },
    },
]
NAMES = {tool["name"] for tool in TOOLS}
MUTATING = {"card_progress", "card_comment", "card_propose", "card_block", "card_submit"}


def connect(path):
    if path.startswith("tcp:"):
        host, port = path[4:].rsplit(":", 1)
        if host != "127.0.0.1":
            raise OSError("board proxy must be on loopback")
        return socket.create_connection((host, int(port)), timeout=60)
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(60)
    connection.connect(path)
    return connection


def call(path, tool, args):
    request = {"tool": tool, "args": args}
    if secret := os.environ.get("NIUCAI_BOARD_SECRET"):
        request["secret"] = secret
    with connect(path) as connection:
        connection.sendall((json.dumps(request, ensure_ascii=False) + "\n").encode())
        buffer = b""
        while not buffer.endswith(b"\n"):
            chunk = connection.recv(65536)
            if not chunk:
                break
            buffer += chunk
    return json.loads(buffer or b'{"ok": false, "error": "runner closed the connection"}')


def handle(path, message):
    method, params = message.get("method"), message.get("params") or {}
    if method == "initialize":
        return {
            "protocolVersion": params.get("protocolVersion") or PROTOCOL,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "niucai-board", "version": "p4"},
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        name, args = params.get("name"), dict(params.get("arguments") or {})
        if name not in NAMES:
            return {"content": [{"type": "text", "text": f"unknown tool: {name}"}], "isError": True}
        if name in MUTATING:
            # Runner 转发失败会重试；同一次调用带同一个幂等键，Kernel 不会重复记事件。
            args.setdefault("idempotency_key", uuid.uuid4().hex)
        try:
            reply = call(path, name, args)
        except OSError as exc:
            reply = {"ok": False, "error": f"runner unreachable: {type(exc).__name__}"}
        text = json.dumps(reply.get("result") if reply.get("ok") else reply.get("error"), ensure_ascii=False)
        return {"content": [{"type": "text", "text": text}], "isError": not reply.get("ok")}
    raise LookupError(method)


def main(path, stdin=sys.stdin, stdout=sys.stdout):
    for line in stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if "id" not in message:
            continue  # notifications（如 notifications/initialized）无需回复
        try:
            reply = {"jsonrpc": "2.0", "id": message["id"], "result": handle(path, message)}
        except LookupError:
            reply = {
                "jsonrpc": "2.0",
                "id": message["id"],
                "error": {"code": -32601, "message": "method not found"},
            }
        stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
        stdout.flush()


if __name__ == "__main__":
    main(sys.argv[1])
