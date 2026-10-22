import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from niucai.actions.adapters import FakeAdapter
from niucai.actions.gateway import ActionGateway
from niucai.api.app import create_app
from niucai.control.conversations import ConversationManager
from niucai.storage.db import Action, Approval, Audit, Conversation, Event, RemoteNode

WRITE = {"type": "files.write", "path": "a.txt", "content": "yolo"}


def started(setup, autonomy=None, mode="agent"):
    db, settings, tasks, _ = setup
    conversation, _, task = ConversationManager(db).send(None, "write a file")
    with db.sessions.begin() as session:
        session.get(Conversation, conversation.id).settings = {
            "mode": mode,
            "autonomy": autonomy or {},
        }
    return conversation, tasks.claim()


async def test_yolo_auto_approves_but_keeps_audit(setup):
    db, settings, _, _ = setup
    settings.yolo_enabled = True
    _, task = started(setup, {"actions": True})
    gateway = ActionGateway(db, settings, FakeAdapter())
    action = gateway.propose(task.id, task.run_token, WRITE, "yolo-write")
    assert action.status == "APPROVED"
    assert (await gateway.execute(action.id, task.run_token)).status == "SUCCEEDED"
    with db.sessions() as session:
        approval = session.scalar(select(Approval).where(Approval.action_id == action.id))
        assert approval.status == "APPROVED" and approval.note == "YOLO 自动批准"
        outcomes = session.scalars(select(Audit.outcome).where(Audit.action_id == action.id)).all()
        assert "AUTO_APPROVED" in outcomes
        events = session.scalars(select(Event.type)).all()
        assert "approval.auto_approved" in events and "approval.requested" not in events


def test_yolo_requires_kernel_switch_and_never_covers_shell(setup):
    db, settings, _, _ = setup
    settings.allow_shell = True
    _, task = started(setup, {"actions": True})
    gateway = ActionGateway(db, settings, FakeAdapter())
    # 总开关关闭时，即便会话残留 YOLO 设置也需要人工审批。
    assert gateway.propose(task.id, task.run_token, WRITE, "off").status == "WAITING_APPROVAL"
    settings.yolo_enabled = True
    shell = gateway.propose(task.id, task.run_token, {"type": "shell.exec", "argv": ["ls"]}, "sh")
    assert shell.status == "WAITING_APPROVAL"


def test_conversation_settings_api(setup):
    db, settings, _, _ = setup
    client = TestClient(
        create_app(settings, db, FakeAdapter()), headers={"Authorization": "Bearer " + "x" * 40}
    )
    conversation = client.post("/api/conversations", json={"title": "c"}).json()
    assert client.get(f"/api/conversations/{conversation['id']}").json()["settings"] == {
        "mode": "agent",
        "autonomy": {"actions": False, "remote": False},
    }
    url = f"/api/conversations/{conversation['id']}"
    assert client.patch(url, json={"settings": {"autonomy": {"actions": True}}}).status_code == 409
    assert client.patch(url, json={"settings": {"mode": "chat"}}).json()["settings"]["mode"] == "chat"
    assert client.patch(url, json={"settings": {"mode": "bogus"}}).status_code == 422
    settings.yolo_enabled = True
    body = client.patch(url, json={"settings": {"autonomy": {"remote": True}}}).json()
    assert body["settings"] == {"mode": "chat", "autonomy": {"actions": False, "remote": True}}
    # 关闭总开关后仍允许关闭 YOLO。
    settings.yolo_enabled = False
    assert client.patch(url, json={"settings": {"autonomy": {"remote": False}}}).status_code == 200


def test_delegated_conversation_inherits_yolo(setup):
    from niucai.control.collaboration import CollaborationManager
    from niucai.storage.db import Agent, Task

    db, settings, tasks, _ = setup
    settings.multi_bot_enabled = settings.yolo_enabled = True
    with db.sessions.begin() as session:
        session.add(Agent(id="writer", name="writer", definition={"tools": ["files.*"]}))
    conversation, root = started(setup, {"actions": True})
    for message in ConversationManager(db).pending(root.id):
        ConversationManager(db).acknowledge(root.id, root.run_token, message.id, message.id)
    handoff = CollaborationManager(db, settings).send(root.id, root.run_token, "t", "writer", "write")
    with db.sessions() as session:
        child = session.get(Task, handoff["to_task_id"])
        assert session.get(Conversation, child.conversation_id).settings["autonomy"]["actions"] is True


@pytest.mark.parametrize("enabled", [True, False])
def test_yolo_remote_jobs_skip_approval(setup, enabled):
    from niucai.control.remote import RemoteManager

    db, settings, _, _ = setup
    settings.remote_pi_enabled = True
    settings.yolo_enabled = True
    _, task = started(setup, {"remote": enabled})
    remote = RemoteManager(db, settings)
    node = remote.create_node("vps")
    with db.sessions() as session:
        assert session.get(RemoteNode, node["id"])
    job = remote.submit("crawl", f"job-{enabled}", (), task.id, task.run_token)
    expected = "QUEUED" if enabled else "WAITING_APPROVAL"
    assert remote.get(job["id"])["status"] == expected
    with db.sessions() as session:
        assert session.scalar(select(Action.id)) is None
