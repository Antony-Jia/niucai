from fastapi.testclient import TestClient
from sqlalchemy import select

from niucai.api.app import create_app
from niucai.domain.schemas import TaskCreate
from niucai.storage.db import Artifact, Conversation, Event, Message, Task


class ReplyGateway:
    def __init__(self, fail=False):
        self.fail, self.calls = fail, []

    async def chat(self, role, messages, task_id):
        self.calls.append((role, messages, task_id))
        if self.fail:
            raise RuntimeError("secret-must-not-leak")
        return {"choices": [{"message": {"content": "把目标创建为 Task，我会持续推进。"}}]}


def test_chat_persistence_and_task_separation(setup):
    db, settings, _, _ = setup
    gateway = ReplyGateway()
    auth = {"Authorization": f"Bearer {settings.api_token.get_secret_value()}"}
    with TestClient(create_app(settings, db, gateway)) as client:
        assert client.post("/api/chat", json={"content": "你好"}).status_code == 401
        first = client.post("/api/chat", json={"content": "请帮我规划"}, headers=auth).json()
        cid = first["conversation"]["id"]
        assert [m["role"] for m in first["messages"]] == ["user", "assistant"]
        response = client.post(
            "/api/chat", json={"content": "再详细一些", "conversation_id": cid}, headers=auth
        )
        assert response.json()["conversation"]["id"] == cid
        assert len(client.get(f"/api/conversations/{cid}/messages", headers=auth).json()) == 4
        assert client.get("/api/conversations", headers=auth).json()[0]["id"] == cid
        assert client.get("/api/tasks", headers=auth).json() == []
        assert gateway.calls[1][0] == "executor"
        assert any(m["content"] == "请帮我规划" for m in gateway.calls[1][1])
    with TestClient(create_app(settings, db, gateway)) as client:
        assert len(client.get(f"/api/conversations/{cid}/messages", headers=auth).json()) == 4
    with db.sessions() as session:
        assert len(list(session.scalars(select(Conversation)))) == 1
        assert len(list(session.scalars(select(Task)))) == 0


def test_failed_and_interrupted_replies_are_explicit(setup):
    db, settings, _, _ = setup
    auth = {"Authorization": f"Bearer {settings.api_token.get_secret_value()}"}
    with TestClient(create_app(settings, db, ReplyGateway(fail=True))) as client:
        result = client.post("/api/chat", json={"content": "你好"}, headers=auth).json()
        cid = result["conversation"]["id"]
        assert result["messages"][-1]["status"] == "FAILED"
        assert "secret-must-not-leak" not in str(result)
        with db.sessions.begin() as session:
            session.add(Message(conversation_id=cid, role="assistant", content="", status="PENDING"))
        assert (
            client.post(
                "/api/chat", json={"content": "下一轮", "conversation_id": cid}, headers=auth
            ).status_code
            == 409
        )
        assert client.post("/api/chat", json={"content": " "}, headers=auth).status_code == 409
        assert client.post("/api/chat", json={"content": "x" * 12001}, headers=auth).status_code == 422


def test_artifact_download_and_latest_event_cursor(setup):
    db, settings, manager, _ = setup
    task = manager.create(TaskCreate(title="Report", goal="Write a report"))
    settings.workspace.mkdir(parents=True)
    (settings.workspace / "report.md").write_text("你好 niucai")
    with db.sessions.begin() as session:
        artifact = Artifact(task_id=task.id, path="report.md", media_type="text/markdown")
        session.add(artifact)
        session.flush()
        aid = artifact.id
        for i in range(220):
            session.add(Event(type="test.event", data={"i": i}))
    auth = {"Authorization": f"Bearer {settings.api_token.get_secret_value()}"}
    with TestClient(create_app(settings, db)) as client:
        assert client.get("/api/artifacts").status_code == 401
        assert client.get("/api/artifacts", headers=auth).json()[0]["id"] == aid
        assert client.get(f"/api/artifacts/{aid}", headers=auth).json()["path"] == "report.md"
        assert client.get(f"/api/artifacts/{aid}/content", headers=auth).text == "你好 niucai"
        recent = client.get("/api/events/recent?limit=10", headers=auth).json()
        assert len(recent) == 10 and recent[0]["data"]["i"] == 219
        assert recent[0]["id"] > recent[-1]["id"]
        assert client.get(f"/api/events?after={recent[0]['id']}", headers=auth).json() == []
