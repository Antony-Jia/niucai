import sys
import textwrap

import pytest
from fastapi.testclient import TestClient

from niucai.actions.adapters import FakeAdapter
from niucai.actions.gateway import ActionGateway
from niucai.api.app import create_app
from niucai.context.compiler import ContextCompiler
from niucai.control.conversations import ConversationManager
from niucai.domain.schemas import TaskCreate
from niucai.storage.db import Agent, Conversation

SERVER = textwrap.dedent("""
    import json, sys

    TOOLS = [
        {"name": "echo", "description": "Echo text", "annotations": {"readOnlyHint": True},
         "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}},
        {"name": "save", "description": "Persist text", "inputSchema": {"type": "object"}},
    ]
    for line in sys.stdin:
        message = json.loads(line)
        if "id" not in message:
            continue
        method, params = message["method"], message.get("params", {})
        if method == "initialize":
            result = {"protocolVersion": params["protocolVersion"], "capabilities": {"tools": {}},
                      "serverInfo": {"name": "test", "version": "1"}}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            text = params["arguments"].get("text", "")
            result = {"content": [{"type": "text", "text": params["name"] + ":" + text}]}
        print("log line that is not json", flush=True)
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
""")


def client_for(setup):
    db, settings, _, _ = setup
    app = create_app(settings, db, FakeAdapter())
    return TestClient(app, headers={"Authorization": "Bearer " + "x" * 40})


def claimed(setup, agent_id=None, autonomy=None):
    db, settings, tasks, _ = setup
    if agent_id:
        settings.multi_bot_enabled = True
        tasks.create(TaskCreate(title="t", goal="crawl the shop", agent_id=agent_id), unified=True)
        task = tasks.claim()
    else:
        conversation, _, _ = ConversationManager(db).send(None, "crawl the shop")
        with db.sessions.begin() as session:
            session.get(Conversation, conversation.id).settings = {"autonomy": autonomy or {}}
        task = tasks.claim()
    return task


async def test_skills_are_readable_by_every_bot(setup):
    db, settings, _, _ = setup
    client = client_for(setup)
    crawl = client.post(
        "/api/skills", json={"name": "crawl-shop", "description": "抓取电商", "content": "先分页"}
    ).json()
    client.post("/api/skills", json={"name": "finance", "content": "x"})
    assert client.post("/api/skills", json={"name": "finance"}).status_code == 409
    patched = client.patch(f"/api/skills/{crawl['id']}", json={"content": "先分页再去重"}).json()
    assert patched["version"] == 2
    with db.sessions.begin() as session:
        # 未声明任何工具的 Bot 也能读技能；skills 白名单收窄目录。
        session.add(Agent(id="scout", name="Scout", definition={"tools": [], "skills": ["crawl-*"]}))
    task = claimed(setup, "scout")
    package = ContextCompiler(db, settings).compile(task.id)
    assert [s["name"] for s in package["skills"]] == ["crawl-shop"]
    assert package["available_tools"] == ["skills.read"]
    gateway = ActionGateway(db, settings, FakeAdapter())
    action = gateway.propose(task.id, task.run_token, {"type": "skills.read", "name": "crawl-shop"}, "k1")
    assert action.risk == "LOW"
    done = await gateway.execute(action.id, task.run_token)
    assert done.status == "SUCCEEDED" and done.result["content"] == "先分页再去重"
    with pytest.raises(PermissionError):
        gateway.propose(task.id, task.run_token, {"type": "skills.read", "name": "finance"}, "k2")
    assert client.delete(f"/api/skills/{crawl['id']}").status_code == 204


async def test_mcp_stdio_server_refresh_and_call(setup, tmp_path):
    db, settings, _, _ = setup
    script = tmp_path / "server.py"
    script.write_text(SERVER)
    client = client_for(setup)
    body = {
        "name": "local",
        "transport": "stdio",
        "config": {"command": sys.executable, "args": [str(script)], "env": {"TOKEN": "secret"}},
    }
    server = client.post("/api/mcp/servers", json=body).json()
    assert server["config"]["env"] == {"TOKEN": "***"}
    url = f"/api/mcp/servers/{server['id']}"
    assert client.post(url + "/refresh").status_code == 403  # MCP 总开关默认关闭
    settings.mcp_enabled = True
    refreshed = client.post(url + "/refresh").json()
    assert refreshed["status"]["ok"] and [t["name"] for t in refreshed["tools"]] == ["echo", "save"]
    # 保留被掩码的密钥。
    client.patch(url, json={"config": {**body["config"], "env": {"TOKEN": "***"}}})
    with db.sessions() as session:
        from niucai.storage.db import McpServer

        assert session.get(McpServer, server["id"]).config["env"] == {"TOKEN": "secret"}
    client.post(url + "/refresh")

    task = claimed(setup)
    package = ContextCompiler(db, settings).compile(task.id)
    assert {t["tool"]: t["read_only"] for t in package["mcp_tools"]} == {"echo": True, "save": False}
    assert "mcp.call" in package["available_tools"]
    gateway = ActionGateway(db, settings, FakeAdapter())
    echo = {"type": "mcp.call", "server": "local", "tool": "echo", "arguments": {"text": "hi"}}
    action = gateway.propose(task.id, task.run_token, echo, "m1")
    assert action.risk == "LOW"
    done = await gateway.execute(action.id, task.run_token)
    assert done.status == "SUCCEEDED", done.result
    assert done.result["content"] == "echo:hi" and not done.result["is_error"]
    save = {"type": "mcp.call", "server": "local", "tool": "save", "arguments": {}}
    assert gateway.propose(task.id, task.run_token, save, "m2").status == "WAITING_APPROVAL"
    with pytest.raises(ValueError):
        gateway.propose(task.id, task.run_token, {**save, "tool": "missing"}, "m3")


def test_mcp_call_follows_yolo_and_server_allowlist(setup):
    db, settings, _, _ = setup
    settings.mcp_enabled = settings.yolo_enabled = True
    from niucai.storage.db import McpServer

    with db.sessions.begin() as session:
        tools = [{"name": "save", "description": "", "input_schema": {}, "annotations": {}}]
        session.add(McpServer(name="local", transport="stdio", config={"command": "x"}, tools=tools))
        session.add(Agent(id="scout", name="Scout", definition={"tools": ["mcp.call"], "mcp_servers": []}))
    task = claimed(setup, autonomy={"actions": True})
    gateway = ActionGateway(db, settings, FakeAdapter())
    save = {"type": "mcp.call", "server": "local", "tool": "save", "arguments": {}}
    assert gateway.propose(task.id, task.run_token, save, "y1").status == "APPROVED"
    _, _, tasks, _ = setup
    inbox = ConversationManager(db)
    for message in inbox.pending(task.id):
        inbox.acknowledge(task.id, task.run_token, message.id, message.id)
    tasks.update(task.id, task.run_token, status="COMPLETED", checkpoint={"result": "ok"})
    scout = claimed(setup, "scout")
    assert "mcp_tools" not in ContextCompiler(db, settings).compile(scout.id)
    with pytest.raises(PermissionError):
        gateway.propose(scout.id, scout.run_token, save, "y2")
