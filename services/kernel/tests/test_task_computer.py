from fastapi.testclient import TestClient
from sqlalchemy import select

from niucai.actions.adapters import FakeAdapter
from niucai.api.app import create_app
from niucai.domain.schemas import Decision, Plan, TaskCreate
from niucai.storage.db import Action, Task
from niucai.worker import Worker


class BrowserRuntime:
    async def plan(self, package, task_id):
        return Plan(goal=package["goal"], steps=[])

    async def decide(self, package, task_id):
        return Decision.model_validate(
            {"kind": "action", "explanation": "observe", "action": {"type": "browser.snapshot"}}
        )


async def test_missing_computer_waits_instead_of_recovering_forever(setup):
    db, settings, manager, cid = setup
    task = manager.create(TaskCreate(title="browser", goal="browse"))
    worker = Worker(db, settings, BrowserRuntime(), FakeAdapter())
    await worker.run_once()
    with db.sessions() as session:
        current = session.get(Task, task.id)
        assert current.status == "WAITING_HUMAN"
        assert current.run_token is None
        assert current.checkpoint["reason"] == "computer required"
        assert list(session.scalars(select(Action))) == []
    assert manager.claim() is None
    manager.attach_computer(task.id, cid)
    manager.transition(task.id, "resume")
    # The saved proposal runs once after choosing a computer.
    settings.max_steps = 1
    await worker.run_once()
    with db.sessions() as session:
        assert session.get(Task, task.id).current_step == 1
        assert len(list(session.scalars(select(Action)))) == 1


def test_attach_computer_requires_auth_and_paused_task(setup):
    db, settings, manager, cid = setup
    task = manager.create(TaskCreate(title="browser", goal="browse"))
    auth = {"Authorization": f"Bearer {settings.api_token.get_secret_value()}"}
    path = f"/api/tasks/{task.id}/computer"
    with TestClient(create_app(settings, db)) as client:
        assert client.put(path, json={"computer_id": cid}).status_code == 401
        claimed = manager.claim()
        assert client.put(path, json={"computer_id": cid}, headers=auth).status_code == 409
        manager.transition(task.id, "pause")
        assert client.put(path, json={"computer_id": "missing"}, headers=auth).status_code == 404
        result = client.put(path, json={"computer_id": cid}, headers=auth)
        assert result.status_code == 200
        assert result.json()["computer_id"] == cid
        assert result.json()["status"] == "PAUSED"
        assert claimed.run_token
