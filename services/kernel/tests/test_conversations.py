from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from niucai.actions.adapters import FakeAdapter
from niucai.actions.gateway import ActionGateway
from niucai.api.app import create_app
from niucai.control.conversations import ConversationManager
from niucai.control.tasks import Conflict
from niucai.storage.db import Action, Approval, Message, Task


def test_concurrent_admission_is_ordered_and_idempotent(setup):
    db, _, _, _ = setup
    inbox = ConversationManager(db)
    conversation, _, task = inbox.send(None, "first", "first")
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: inbox.send(conversation.id, "same", "same"), range(4)))
    assert len({r[1].id for r in results}) == 1
    assert all(r[2].id == task.id for r in results)
    with db.sessions() as session:
        assert len(session.scalars(select(Task)).all()) == 1
        assert [m.sequence for m in session.scalars(select(Message).order_by(Message.sequence))] == [1, 2]


def test_completion_race_keeps_undelivered_message_runnable(setup):
    db, _, tasks, _ = setup
    inbox = ConversationManager(db)
    conversation, first, task = inbox.send(None, "first")
    claimed = tasks.claim()
    inbox.acknowledge(task.id, claimed.run_token, first.id, "one")
    _, second, _ = inbox.send(conversation.id, "changed")
    result = tasks.update(
        task.id, claimed.run_token, status="COMPLETED", checkpoint={"result": "first answer"}
    )
    assert result.status == "PENDING"
    assert result.checkpoint["pi_continue_inbox"]
    assert [m.id for m in inbox.pending(task.id)] == [second.id]
    claimed = tasks.claim()
    inbox.acknowledge(task.id, claimed.run_token, second.id, "two")
    tasks.update(task.id, claimed.run_token, status="COMPLETED", checkpoint={"result": "changed answer"})
    _, _, next_task = inbox.send(conversation.id, "next round")
    assert next_task.id != task.id
    assert next_task.conversation_id == task.conversation_id


@pytest.mark.parametrize("operation", ["pause", "fail", "wait"])
def test_messages_do_not_implicitly_resume_blocked_execution(setup, operation):
    db, _, tasks, _ = setup
    inbox = ConversationManager(db)
    conversation, _, task = inbox.send(None, "first")
    claimed = tasks.claim()
    if operation == "pause":
        tasks.transition(task.id, "pause")
        expected = "PAUSED"
    else:
        expected = "FAILED" if operation == "fail" else "WAITING_HUMAN"
        tasks.update(
            task.id, claimed.run_token, status=expected, checkpoint={"reason": "agent requested human"}
        )
    _, _, same = inbox.send(conversation.id, "follow-up")
    assert same.id == task.id and same.status == expected
    assert tasks.claim() is None


async def test_steering_invalidates_approval_but_preserves_unknown(setup):
    db, settings, tasks, _ = setup
    inbox = ConversationManager(db)
    conversation, _, task = inbox.send(None, "write")
    claimed = tasks.claim()
    gateway = ActionGateway(db, settings, FakeAdapter())
    proposed = gateway.propose(
        task.id, claimed.run_token, {"type": "files.write", "path": "a.txt", "content": "old"}, "old"
    )
    with db.sessions.begin() as session:
        unknown = Action(
            task_id=task.id,
            agent_id="main",
            idempotency_key="unknown",
            spec={"type": "files.read", "path": "x"},
            risk="LOW",
            status="UNKNOWN",
        )
        session.add(unknown)
    inbox.send(conversation.id, "change the content")
    with db.sessions() as session:
        approval = session.scalar(select(Approval).where(Approval.action_id == proposed.id))
        assert approval.status == "DENIED"
        assert session.get(Action, proposed.id).status == "DENIED"
        assert session.get(Action, unknown.id).status == "UNKNOWN"
    with pytest.raises(Conflict):
        gateway.decide(approval.id, True)
    assert (await gateway.execute(proposed.id, claimed.run_token)).status == "DENIED"


def test_timeline_pagination_and_history_are_conversation_scoped(setup):
    db, settings, _, _ = setup
    inbox = ConversationManager(db)
    conversation, _, _ = inbox.send(None, "first")
    inbox.send(conversation.id, "second")
    inbox.send(None, "unrelated")
    auth = {"Authorization": f"Bearer {settings.api_token.get_secret_value()}"}
    with TestClient(create_app(settings, db)) as client:
        items, after = [], 0
        while True:
            page = client.get(
                f"/api/conversations/{conversation.id}/timeline?after={after}&limit=1", headers=auth
            ).json()
            items.extend(page["items"])
            after = page["next_cursor"]
            if not page["has_more"]:
                break
        assert len({item["id"] for item in items}) == len(items)
        assert [item["message"]["content"] for item in items if "message" in item] == ["first", "second"]
        assert client.get(f"/api/conversations/{conversation.id}/timeline").status_code == 401
