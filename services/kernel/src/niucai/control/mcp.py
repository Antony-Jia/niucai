"""Kernel 托管的最小 MCP 客户端（stdio / Streamable HTTP）与 mcp.call 策略。

每次调用新建一个 MCP 会话（initialize → 请求 → 关闭），不在 Kernel 内长期保留连接；
工具清单仅在刷新时缓存到 mcp_servers.tools。stdio 服务器以 API/Worker 进程身份运行，
不是安全沙箱，只应登记可信命令。
"""

import asyncio
import json
import os

import httpx
from sqlalchemy import select

from niucai.control.bots import allows
from niucai.control.tasks import Missing, require
from niucai.storage.db import McpServer, emit, now

PROTOCOL = "2025-06-18"
CLIENT = {"name": "niucai-kernel", "version": "1"}
CATALOG_LIMIT = 60
SCHEMA_CHARS = 3000
SECRET = "***"


class McpError(RuntimeError):
    pass


def initialize(ident):
    return {
        "jsonrpc": "2.0",
        "id": ident,
        "method": "initialize",
        "params": {"protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": CLIENT},
    }


def unwrap(message):
    if "error" in message:
        error = message["error"] or {}
        raise McpError(f"MCP error {error.get('code')}: {str(error.get('message'))[:500]}")
    return message.get("result") or {}


async def stdio(config, method, params):
    env = {k: os.environ[k] for k in ("PATH", "LANG", "TZ", "SYSTEMROOT") if k in os.environ}
    process = await asyncio.create_subprocess_exec(
        config["command"],
        *config.get("args", []),
        cwd=config.get("cwd") or None,
        env={**env, **config.get("env", {})},
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        limit=8 * 1024 * 1024,
    )

    async def send(message):
        process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode())
        await process.stdin.drain()

    async def receive(ident):
        while line := await process.stdout.readline():
            try:
                message = json.loads(line)
            except ValueError:
                continue  # 非协议输出（日志等）忽略
            if message.get("id") == ident and "method" not in message:
                return unwrap(message)
            if "method" in message and "id" in message:
                # 服务器发起的请求：只应答 ping，其余能力未声明。
                reply = (
                    {"result": {}}
                    if message["method"] == "ping"
                    else {"error": {"code": -32601, "message": "not supported"}}
                )
                await send({"jsonrpc": "2.0", "id": message["id"], **reply})
        raise McpError("MCP server exited before responding")

    try:
        await send(initialize(1))
        await receive(1)
        await send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        await send({"jsonrpc": "2.0", "id": 2, "method": method, "params": params})
        return await receive(2)
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()


async def http(config, timeout, method, params):
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        **config.get("headers", {}),
    }
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:

        async def post(message):
            ident = message.get("id")
            async with client.stream("POST", config["url"], json=message, headers=headers) as response:
                if response.status_code >= 400:
                    raise McpError(f"MCP HTTP {response.status_code}")
                if session := response.headers.get("mcp-session-id"):
                    headers["Mcp-Session-Id"] = session
                if ident is None:
                    return None
                if response.headers.get("content-type", "").startswith("text/event-stream"):
                    data = []
                    async for line in response.aiter_lines():
                        if line.startswith("data:"):
                            data.append(line[5:].strip())
                        elif not line and data:
                            event, data = json.loads("\n".join(data)), []
                            if event.get("id") == ident and "method" not in event:
                                return unwrap(event)
                    raise McpError("MCP stream closed before responding")
                body = json.loads(await response.aread())
                for event in body if isinstance(body, list) else [body]:
                    if event.get("id") == ident:
                        return unwrap(event)
                raise McpError("MCP response missing request id")

        await post(initialize(1))
        headers["MCP-Protocol-Version"] = PROTOCOL
        await post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        try:
            return await post({"jsonrpc": "2.0", "id": 2, "method": method, "params": params})
        finally:
            if "Mcp-Session-Id" in headers:
                try:
                    await client.delete(config["url"], headers=headers)
                except httpx.HTTPError:
                    pass


async def request(transport, config, timeout, method, params):
    call = stdio(config, method, params) if transport == "stdio" else http(config, timeout, method, params)
    return await asyncio.wait_for(call, timeout)


async def list_tools(transport, config, timeout):
    tools, cursor = [], None
    for _ in range(10):
        result = await request(transport, config, timeout, "tools/list", {"cursor": cursor} if cursor else {})
        tools.extend(result.get("tools", []))
        cursor = result.get("nextCursor")
        if not cursor:
            break
    return [
        {
            "name": str(tool["name"])[:200],
            "description": str(tool.get("description") or "")[:1000],
            "input_schema": tool.get("inputSchema") or {"type": "object"},
            "annotations": tool.get("annotations") or {},
        }
        for tool in tools
        if tool.get("name")
    ]


async def refresh(db, settings, server_id):
    with db.sessions() as session:
        server = require(session, McpServer, server_id)
        transport, config = server.transport, dict(server.config)
    try:
        tools = await list_tools(transport, config, settings.mcp_timeout)
        status = {"ok": True, "tool_count": len(tools), "refreshed_at": now().isoformat()}
    except Exception as exc:
        tools, status = None, {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
        status["refreshed_at"] = now().isoformat()
    with db.sessions.begin() as session:
        server = require(session, McpServer, server_id, lock=True)
        if tools is not None:
            server.tools = tools
        server.status = status
        emit(session, "mcp.refreshed", None, server_id=server.id, ok=status["ok"])
        return public(server)


def public(server):
    """对外返回时隐藏 env / headers 的值。"""
    config = dict(server.config)
    for field in ("env", "headers"):
        if field in config:
            config[field] = {k: SECRET for k in config[field]}
    return {
        "id": server.id,
        "name": server.name,
        "transport": server.transport,
        "config": config,
        "tools": server.tools,
        "enabled": bool(server.enabled),
        "auto_approve_read_only": bool(server.auto_approve_read_only),
        "status": server.status,
        "created_at": server.created_at,
        "updated_at": server.updated_at,
    }


def keep_secrets(old, new):
    """PATCH 时值为 *** 的 env / headers 保留原值。"""
    merged = dict(new)
    for field in ("env", "headers"):
        if field in merged:
            merged[field] = {
                k: old.get(field, {}).get(k, "") if v == SECRET else v for k, v in merged[field].items()
            }
    return merged


def visible(identity, name):
    return identity.mcp_servers is None or allows(identity.mcp_servers, name)


def catalog(session, identity):
    tools = []
    for server in session.scalars(select(McpServer).where(McpServer.enabled == 1).order_by(McpServer.name)):
        if not visible(identity, server.name):
            continue
        for tool in server.tools:
            schema = tool.get("input_schema") or {"type": "object"}
            if len(json.dumps(schema, ensure_ascii=False)) > SCHEMA_CHARS:
                schema = {"type": "object", "note": "schema too large; pass arguments per description"}
            tools.append(
                {
                    "server": server.name,
                    "tool": tool["name"],
                    "description": tool.get("description", "")[:300],
                    "read_only": (tool.get("annotations") or {}).get("readOnlyHint") is True,
                    "input_schema": schema,
                }
            )
    return tools[:CATALOG_LIMIT]


def resolve(session, settings, identity, spec):
    if not settings.mcp_enabled:
        raise PermissionError("MCP disabled by kernel policy")
    server = session.scalar(select(McpServer).where(McpServer.name == spec["server"]))
    if server is None:
        raise Missing(f"MCP server {spec['server']} not found")
    if not server.enabled:
        raise PermissionError(f"MCP server {server.name} is disabled")
    if not visible(identity, server.name):
        raise PermissionError(f"Bot configuration denies MCP server {server.name}")
    tool = next((t for t in server.tools if t["name"] == spec["tool"]), None)
    if tool is None:
        raise ValueError(f"unknown MCP tool {spec['tool']}; refresh the server tool list")
    return server, tool


def call_risk(session, settings, identity, spec):
    server, tool = resolve(session, settings, identity, spec)
    read_only = (tool.get("annotations") or {}).get("readOnlyHint") is True
    return "LOW" if read_only and server.auto_approve_read_only else "HIGH"


def flatten(result, limit):
    parts = []
    for item in result.get("content") or []:
        if item.get("type") == "text":
            parts.append(str(item.get("text", "")))
        elif item.get("type") == "resource" and "text" in (item.get("resource") or {}):
            parts.append(str(item["resource"]["text"]))
        else:
            parts.append(f"[{item.get('type', 'content')} omitted]")
    text = "\n".join(parts)
    output = {"content": text[:limit], "truncated": len(text) > limit}
    output["is_error"] = bool(result.get("isError"))
    structured = result.get("structuredContent")
    if structured is not None and len(json.dumps(structured, ensure_ascii=False)) <= limit:
        output["structured"] = structured
    return output


async def call(settings, transport, config, spec):
    params = {"name": spec["tool"], "arguments": spec.get("arguments") or {}}
    result = await request(transport, config, settings.mcp_timeout, "tools/call", params)
    return {"server": spec["server"], "tool": spec["tool"], **flatten(result, settings.tool_result_chars)}


KERNEL_ACTIONS = {"skills.read", "mcp.call"}


def kernel_execute(session, settings, identity, spec):
    """返回在 Kernel 内执行 skills.read / mcp.call 的协程（不经电脑适配器）。"""
    if spec["type"] == "skills.read":
        from niucai.control.skills import read

        result = read(session, settings, identity, spec["name"])

        async def done():
            return result

        return done()
    server, _ = resolve(session, settings, identity, spec)
    return call(settings, server.transport, dict(server.config), spec)
