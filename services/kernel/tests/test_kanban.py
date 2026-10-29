from fastapi.testclient import TestClient

from niucai.api.app import create_app
from niucai.control.remote import RemoteManager
from niucai.domain.schemas import TaskCreate
from niucai.storage.db import Task


def test_kanban_shows_tasks_agents_and_busy_nodes(setup):
    db, settings, tasks, _ = setup
    settings.remote_pi_enabled = True
    auth = {"Authorization": f"Bearer {settings.api_token.get_secret_value()}"}
    task = tasks.create(TaskCreate(title="board", goal="demo goal"), unified=True)
    with db.sessions.begin() as s:
        s.get(Task, task.id).status = "RUNNING"
    manager = RemoteManager(db, settings)
    node = manager.create_node("vps-1")
    manager.node_heartbeat(node["id"], {"executor": "pi", "available": True})
    job = manager.submit("run it", "k1", [], task.id)
    manager.control(job["id"], "approve")
    claimed = manager.claim(node["id"])
    assert claimed
    with TestClient(create_app(settings, db)) as client:
        assert client.get("/api/kanban").status_code == 401
        board = client.get("/api/kanban", headers=auth).json()
    card = next(c for c in board["tasks"] if c["id"] == task.id)
    assert card["agents"][0] == {"id": "main", "name": "Main", "role": "lead"}
    assert card["remote_jobs"][0]["node_name"] == "vps-1"
    vps = next(r for r in board["resources"] if r["id"] == node["id"])
    assert vps["busy"] and vps["job"]["task_title"] == "board"
