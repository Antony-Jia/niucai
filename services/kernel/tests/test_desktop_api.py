from fastapi.testclient import TestClient
from sqlalchemy import select

from niucai.api.app import create_app
from niucai.domain.schemas import TaskCreate
from niucai.storage.db import Artifact, Conversation, Event, Task


class ReplyGateway:
    def __init__(self, fail=False):
        self.fail, self.calls = fail, []

    async def chat(self, role, messages, task_id):
        self.calls.append((role, messages, task_id))
        if self.fail:
            raise RuntimeError("secret-must-not-leak")
        return {"choices": [{"message": {"content": "把目标创建为 Task，我会持续推进。"}}]}


def test_chat_admits_execution_without_calling_model(setup):
    db, settings, _, _ = setup
    gateway = ReplyGateway()
    auth = {"Authorization": f"Bearer {settings.api_token.get_secret_value()}"}
    with TestClient(create_app(settings, db, gateway)) as client:
        assert client.post("/api/chat", json={"content": "hello"}).status_code == 401
        response = client.post("/api/chat", json={"content": "plan", "client_id": "one"}, headers=auth)
        assert response.status_code == 202
        first = response.json()
        cid = first["conversation"]["id"]
        assert [m["role"] for m in first["messages"]] == ["user"]
        assert first["task"]["status"] == "PENDING"
        duplicate = client.post(
            f"/api/conversations/{cid}/messages", json={"content": "plan", "client_id": "one"}, headers=auth
        ).json()
        assert duplicate["messages"][0]["id"] == first["messages"][0]["id"]
        second = client.post(
            f"/api/conversations/{cid}/messages", json={"content": "more", "client_id": "two"}, headers=auth
        ).json()
        assert second["task"]["id"] == first["task"]["id"]
        assert second["messages"][0]["sequence"] == 2
        assert len(client.get("/api/tasks", headers=auth).json()) == 1
        assert gateway.calls == []
    with TestClient(create_app(settings, db, gateway)) as client:
        assert len(client.get(f"/api/conversations/{cid}/messages", headers=auth).json()) == 2
    with db.sessions() as session:
        assert len(list(session.scalars(select(Conversation)))) == 1
        assert len(list(session.scalars(select(Task)))) == 1


def test_invalid_and_reused_message_ids(setup):
    db, settings, _, _ = setup
    auth = {"Authorization": f"Bearer {settings.api_token.get_secret_value()}"}
    with TestClient(create_app(settings, db)) as client:
        result = client.post("/api/chat", json={"content": "hello", "client_id": "one"}, headers=auth).json()
        cid = result["conversation"]["id"]
        assert (
            client.post(
                f"/api/conversations/{cid}/messages",
                json={"content": "different", "client_id": "one"},
                headers=auth,
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
