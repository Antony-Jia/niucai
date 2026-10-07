"""Pi-specific ownership, retry, budget and migration integration checks."""

import asyncio
import fcntl
from hashlib import sha256

import pytest
from sqlalchemy import select
from test_agent_sessions import Computer, Model, call, state

from niucai.agents.pi_adapter import PiDurableRuntime
from niucai.agents.selection import RuntimeRouter
from niucai.control.tasks import Conflict
from niucai.domain.schemas import TaskCreate
from niucai.storage.db import Approval, Task
from niucai.worker import Worker


async def test_browser_without_computer_can_attach_and_resume_same_tool(setup):
    db, settings, manager, cid = setup
    task = manager.create(TaskCreate(title="browser", goal="observe"))
    model = Model(call("observe", {"type": "browser.snapshot"}), {"content": "done"})
    computer = Computer()
    worker = Worker(db, settings, PiDurableRuntime(model), computer)
    await worker.run_once()
    before = state(db, task.id)
    assert before.status == "WAITING_HUMAN"
    assert before.checkpoint["reason"] == "computer required"
    assert not computer.calls
    manager.attach_computer(task.id, cid)
    manager.transition(task.id, "resume")
    await worker.run_once()
    after = state(db, task.id)
    assert after.status == "COMPLETED"
    assert after.checkpoint["pi_submission_id"] == before.checkpoint["pi_submission_id"]
    assert len(computer.calls) == 1


async def test_model_turn_budget_preserves_pending_generation(setup):
    db, settings, manager, cid = setup
    settings.max_model_turns = 1
    task = manager.create(TaskCreate(title="budget", goal="observe", computer_id=cid))
    model = Model(call("one", {"type": "browser.snapshot"}), {"content": "done"})
    computer = Computer()
    worker = Worker(db, settings, PiDurableRuntime(model), computer)
    await worker.run_once()
    assert state(db, task.id).status == "WAITING_HUMAN"
    assert state(db, task.id).checkpoint["reason"] == "model turn budget exhausted"
    manager.transition(task.id, "resume")
    await worker.run_once()
    assert state(db, task.id).status == "COMPLETED"
    assert len(computer.calls) == 1
    assert len(model.messages) == 2


async def test_failed_submission_retry_continues_same_conversation(setup):
    db, settings, manager, cid = setup
    task = manager.create(TaskCreate(title="retry", goal="answer", computer_id=cid))
    broken = call("bad", {"type": "browser.snapshot"})
    broken["tool_calls"][0]["function"]["arguments"] = "{broken"
    model = Model(broken, {"content": "recovered"})
    worker = Worker(db, settings, PiDurableRuntime(model), Computer())
    await worker.run_once()
    before = state(db, task.id)
    assert before.status == "FAILED"
    manager.transition(task.id, "retry")
    await worker.run_once()
    after = state(db, task.id)
    assert after.status == "COMPLETED"
    assert after.checkpoint["pi_conversation_id"] == before.checkpoint["pi_conversation_id"]
    assert after.checkpoint["pi_submission_id"] != before.checkpoint["pi_submission_id"]
    assert after.checkpoint["result"] == "recovered"


async def test_existing_writer_prevents_second_harness(setup):
    db, settings, manager, cid = setup
    task = manager.create(TaskCreate(title="lock", goal="answer", computer_id=cid))
    directory = settings.pi_storage / sha256(task.id.encode()).hexdigest()
    directory.mkdir(parents=True)
    model = Model({"content": "done"})
    worker = Worker(db, settings, PiDurableRuntime(model), Computer())
    with (directory / "writer.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert await worker.run_once()
        assert state(db, task.id).status == "RUNNING"
        assert not model.messages
    manager.transition(task.id, "pause")
    manager.transition(task.id, "resume")
    assert await worker.run_once()
    assert state(db, task.id).status == "COMPLETED"


async def test_lease_monitor_cancels_blocked_gateway_and_releases_writer(setup):
    db, settings, manager, cid = setup
    task = manager.create(TaskCreate(title="pause", goal="answer", computer_id=cid))
    started, cancelled = asyncio.Event(), asyncio.Event()

    class Blocked(Model):
        async def chat(self, *args, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    worker = Worker(db, settings, PiDurableRuntime(Blocked()), Computer())
    running = asyncio.create_task(worker.run_once())
    await asyncio.wait_for(started.wait(), 5)
    manager.transition(task.id, "pause")
    await asyncio.wait_for(running, 3)
    assert cancelled.is_set()
    assert state(db, task.id).status == "PAUSED"
    manager.transition(task.id, "resume")
    resumed = Worker(db, settings, PiDurableRuntime(Model({"content": "done"})), Computer())
    assert await resumed.run_once()
    assert state(db, task.id).status == "COMPLETED"


async def test_router_pins_runtime_and_preserves_legacy_graph(setup):
    from niucai.agents.checkpointer import DatabaseSaver
    from niucai.agents.deepagent_adapter import DeepAgentRuntime

    db, settings, manager, cid = setup
    settings.runtime = "pi"
    task = manager.create(TaskCreate(title="old", goal="await approval", computer_id=cid))
    model = Model(call("write", {"type": "files.write", "path": "a", "content": "x"}))
    worker = Worker(db, settings, DeepAgentRuntime(model), Computer())
    await worker.run_once()
    with db.sessions() as s:
        approval = s.scalar(select(Approval))
    worker.actions.decide(approval.id, True)
    claimed = manager.claim()
    router = RuntimeRouter(model)
    assert isinstance(router.select(worker, claimed), DeepAgentRuntime)
    assert state(db, task.id).checkpoint["runtime"] == "deepagents"
    manager.transition(task.id, "pause")
    with pytest.raises(Conflict):
        await DatabaseSaver(db, task.id, claimed.run_token).aput(
            {"configurable": {"thread_id": task.id}}, {"id": "stale"}, {}, {}
        )
    fresh = manager.create(TaskCreate(title="new", goal="answer"))
    claimed = manager.claim()
    assert claimed.id == fresh.id
    assert isinstance(router.select(worker, claimed), PiDurableRuntime)
    settings.runtime = "structured"
    with db.sessions() as session:
        pinned = session.get(Task, fresh.id)
    assert isinstance(router.select(worker, pinned), PiDurableRuntime)


async def test_session_storage_cannot_be_agent_workspace(setup):
    db, settings, manager, cid = setup
    settings.pi_storage = settings.workspace / "sessions"
    task = manager.create(TaskCreate(title="storage", goal="answer", computer_id=cid))
    worker = Worker(db, settings, PiDurableRuntime(Model()), Computer())
    await worker.run_once()
    assert state(db, task.id).status == "FAILED"
    assert "outside the agent workspace" in state(db, task.id).checkpoint["detail"]


async def test_crash_does_not_implicitly_answer_human_wait(setup, monkeypatch):
    import json
    from datetime import timedelta

    from niucai.storage.db import now

    db, settings, manager, cid = setup
    task = manager.create(TaskCreate(title="login", goal="manual login", computer_id=cid))
    model = Model(
        {
            "tool_calls": [
                {
                    "id": "login",
                    "type": "function",
                    "function": {
                        "name": "wait_for_human",
                        "arguments": json.dumps({"reason": "Please log in"}),
                    },
                }
            ]
        },
        {"content": "resumed"},
    )
    worker = Worker(db, settings, PiDurableRuntime(model), Computer())
    original = worker.tasks.update

    class Crash(BaseException):
        pass

    def crash(task_id, token, **changes):
        if changes.get("status") == "WAITING_HUMAN":
            raise Crash()
        return original(task_id, token, **changes)

    monkeypatch.setattr(worker.tasks, "update", crash)
    with pytest.raises(Crash):
        await worker.run_once()
    with db.sessions.begin() as session:
        session.get(Task, task.id).lease_until = now() - timedelta(seconds=1)
    restarted = Worker(db, settings, PiDurableRuntime(model), Computer())
    await restarted.run_once()
    assert state(db, task.id).status == "WAITING_HUMAN"
    assert len(model.messages) == 1
    manager.transition(task.id, "resume")
    await restarted.run_once()
    assert state(db, task.id).status == "COMPLETED"


async def test_compaction_uses_kernel_summarizer_role(setup):
    from niucai.domain.schemas import Role

    db, settings, manager, cid = setup
    task = manager.create(TaskCreate(title="compact", goal="observe then answer", computer_id=cid))

    class CompactionModel(Model):
        summaries = 0

        async def chat(self, role, messages, task_id, **extra):
            if role == Role.SUMMARIZER:
                self.summaries += 1
                return {
                    "choices": [{"message": {"content": "Observed browser successfully; finish answer."}}]
                }
            response = await super().chat(role, messages, task_id, **extra)
            response["usage"] = {"prompt_tokens": 60000, "completion_tokens": 100, "total_tokens": 60100}
            return response

    settings.tool_result_chars = 200000

    class LargeComputer(Computer):
        async def execute(self, spec, computer):
            self.calls.append(spec)
            return {"observation": "large observed page content " * 4000}

    model = CompactionModel(
        call("first", {"type": "browser.snapshot"}),
        call("second", {"type": "browser.snapshot"}),
        {"content": "done"},
    )
    worker = Worker(db, settings, PiDurableRuntime(model), LargeComputer())
    await worker.run_once()
    assert state(db, task.id).status == "COMPLETED"
    assert model.summaries >= 1


async def test_missing_session_volume_fails_without_replanning(setup):
    import shutil

    db, settings, manager, cid = setup
    task = manager.create(TaskCreate(title="lost", goal="write", computer_id=cid))
    model = Model(call("write", {"type": "files.write", "path": "a", "content": "x"}))
    computer = Computer()
    worker = Worker(db, settings, PiDurableRuntime(model), computer)
    await worker.run_once()
    assert state(db, task.id).status == "WAITING_HUMAN"
    shutil.rmtree(settings.pi_storage)
    with db.sessions() as s:
        approval = s.scalar(select(Approval))
    worker.actions.decide(approval.id, True)
    await worker.run_once()
    after = state(db, task.id)
    assert after.status == "FAILED"
    assert "Pi session storage missing" in after.checkpoint["detail"]
    assert len(model.messages) == 1
    assert not computer.calls
