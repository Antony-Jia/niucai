"""Real Pi Durable transport/session tests with deterministic model and computer I/O."""

import asyncio

import pytest
from sqlalchemy import select

pytest.importorskip("fcntl")

from test_agent_sessions import Computer, Model, call, state

from niucai.agents.pi_adapter import PiDurableRuntime
from niucai.control.conversations import ConversationManager
from niucai.storage.db import Action, Approval, Message
from niucai.worker import Worker


async def test_chat_and_followup_share_pi_history_without_planner(setup):
    db, settings, _, _ = setup
    inbox = ConversationManager(db)
    conversation, _, task = inbox.send(None, "Remember the word blue")
    model = Model({"content": "Remembered blue"}, {"content": "blue"})
    worker = Worker(db, settings, PiDurableRuntime(model), Computer())
    await worker.run_once()
    first = state(db, task.id)
    assert first.status == "COMPLETED" and not first.plan
    _, _, second = inbox.send(conversation.id, "What word did I ask you to remember?")
    await worker.run_once()
    assert state(db, second.id).status == "COMPLETED"
    assert state(db, second.id).checkpoint["pi_conversation_id"] == first.checkpoint["pi_conversation_id"]
    assert any(m.get("content") == "Remembered blue" for m in model.messages[-1])
    with db.sessions() as session:
        assert [m.role for m in session.scalars(select(Message).order_by(Message.sequence))] == [
            "user",
            "assistant",
            "user",
            "assistant",
        ]
        assert all(m.delivered_at for m in session.scalars(select(Message).where(Message.role == "user")))


async def test_steering_during_model_request_fences_old_action(setup):
    db, settings, _, cid = setup
    inbox = ConversationManager(db)
    conversation, _, task = inbox.send(None, "Observe the browser", computer_id=cid)
    entered, release = asyncio.Event(), asyncio.Event()

    class BlockingModel(Model):
        async def chat(self, role, messages, task_id, **extra):
            if not self.messages:
                entered.set()
                await release.wait()
            return await super().chat(role, messages, task_id, **extra)

    model = BlockingModel(
        call("old", {"type": "browser.navigate", "url": "https://example.com"}),
        {"content": "I will only discuss it"},
    )
    computer = Computer()
    worker = Worker(db, settings, PiDurableRuntime(model), computer)
    execution = asyncio.create_task(worker.run_once())
    await asyncio.wait_for(entered.wait(), 10)
    inbox.send(conversation.id, "Only discuss it. Do not use the computer.")
    await asyncio.sleep(0.3)
    release.set()
    await asyncio.wait_for(execution, 15)
    assert state(db, task.id).status == "COMPLETED"
    assert computer.calls == []
    assert any("Only discuss it" in str(m.get("content", "")) for m in model.messages[-1])


async def test_acknowledgement_loss_replays_same_submission_without_duplicate_model_call(setup, monkeypatch):
    db, settings, tasks, _ = setup
    inbox = ConversationManager(db)
    _, _, task = inbox.send(None, "Answer once")
    model = Model({"content": "one answer"})
    worker = Worker(db, settings, PiDurableRuntime(model), Computer())
    original = ConversationManager.acknowledge
    failed = False

    def fail_once(self, *args):
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("simulated crash after durable Pi admission")
        return original(self, *args)

    monkeypatch.setattr(ConversationManager, "acknowledge", fail_once)
    await worker.run_once()
    assert state(db, task.id).status == "FAILED"
    tasks.transition(task.id, "retry")
    await worker.run_once()
    assert state(db, task.id).status == "COMPLETED"
    assert len(model.messages) == 1
    with db.sessions() as session:
        assert not list(session.scalars(select(Action)))


async def test_cancelled_tools_cannot_execute_in_next_conversation_round(setup):
    db, settings, tasks, _ = setup
    inbox = ConversationManager(db)
    conversation, _, first = inbox.send(None, "write an old file")
    model = Model(
        call("old", {"type": "files.write", "path": "old.txt", "content": "old"}),
        {"content": "new work only"},
    )
    computer = Computer()
    worker = Worker(db, settings, PiDurableRuntime(model), computer)
    await worker.run_once()
    assert state(db, first.id).status == "WAITING_HUMAN"
    tasks.transition(first.id, "cancel")
    _, _, second = inbox.send(conversation.id, "Just answer, do not write")
    await worker.run_once()
    assert state(db, second.id).status == "COMPLETED"
    assert computer.calls == []
    with db.sessions() as session:
        assert session.scalar(select(Approval)).status == "PENDING"


async def test_acknowledged_steering_survives_worker_pause_and_restart(setup):
    db, settings, tasks, cid = setup
    inbox = ConversationManager(db)
    conversation, _, task = inbox.send(None, "Observe", computer_id=cid)
    entered = asyncio.Event()

    class BlockingModel(Model):
        async def chat(self, role, messages, task_id, **extra):
            if not self.messages and not entered.is_set():
                entered.set()
                await asyncio.Event().wait()
            return await super().chat(role, messages, task_id, **extra)

    model = BlockingModel(call("old", {"type": "browser.snapshot"}), {"content": "updated"})
    worker = Worker(db, settings, PiDurableRuntime(model), Computer())
    execution = asyncio.create_task(worker.run_once())
    await asyncio.wait_for(entered.wait(), 10)
    _, steering, _ = inbox.send(conversation.id, "Updated instruction after restart")
    for _ in range(100):
        with db.sessions() as session:
            if session.get(Message, steering.id).delivered_at:
                break
        await asyncio.sleep(0.05)
    else:
        pytest.fail("steering not acknowledged")
    tasks.transition(task.id, "pause")
    await asyncio.wait_for(execution, 10)
    tasks.transition(task.id, "resume")
    await asyncio.wait_for(worker.run_once(), 15)
    assert state(db, task.id).status == "COMPLETED"
    assert "Updated instruction after restart" in str(model.messages[-1])


async def test_repeated_failed_submissions_do_not_override_successful_retry(setup):
    db, settings, tasks, _ = setup
    inbox = ConversationManager(db)
    _, _, task = inbox.send(None, "Answer after repairing malformed tool arguments")
    broken = call("bad", {"type": "browser.snapshot"})
    broken["tool_calls"][0]["function"]["arguments"] = "{broken"
    model = Model(broken, broken, {"content": "recovered"})
    worker = Worker(db, settings, PiDurableRuntime(model), Computer())
    for _ in range(2):
        await worker.run_once()
        assert state(db, task.id).status == "FAILED"
        tasks.transition(task.id, "retry")
    await worker.run_once()
    assert state(db, task.id).status == "COMPLETED"
    assert state(db, task.id).checkpoint["result"] == "recovered"
    assert len(model.messages) == 3
