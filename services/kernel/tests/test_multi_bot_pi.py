"""Actual durable Pi subprocesses with deterministic Kernel model responses."""

import json
import os
from pathlib import Path

import pytest

pytest.importorskip("fcntl", reason="Pi writer locking requires Linux")

from niucai.actions.adapters import FakeAdapter
from niucai.agents.pi_adapter import PiDurableRuntime
from niucai.domain.schemas import TaskCreate
from niucai.storage.db import Agent, Computer, Handoff, Task, TaskRun, Workflow, WorkItem, as_dict
from niucai.worker import Worker


def tool(name, arguments, ident):
    return {
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": ident,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(arguments),
                            },
                        }
                    ],
                }
            }
        ]
    }


async def test_pi_parallel_delegation_replays_wait_after_restart(setup):
    db, settings, manager, _ = setup
    settings.multi_bot_enabled = True
    with db.sessions.begin() as session:
        for ident in ["writer", "reviewer"]:
            session.add(Agent(id=ident, name=ident, definition={"tools": ["bot_reply"]}))
    root = manager.create(TaskCreate(title="aggregate", goal="delegate B and C then aggregate"), unified=True)

    class Model:
        root_calls = 0

        async def chat(self, role, messages, task_id, **extra):
            with db.sessions() as session:
                agent_id = session.get(Task, task_id).agent_id
            if agent_id != "main":
                return {"choices": [{"message": {"content": f"result from {agent_id}"}}]}
            self.root_calls += 1
            if self.root_calls <= 2:
                return tool(
                    "bot_send",
                    {"to": ["writer", "reviewer"][self.root_calls - 1], "content": "return a result"},
                    f"send-{self.root_calls}",
                )
            if self.root_calls == 3:
                from sqlalchemy import select

                from niucai.storage.db import Handoff

                with db.sessions() as session:
                    ids = list(session.scalars(select(Handoff.id)))
                return tool("bot_wait", {"handoff_ids": ids, "mode": "all"}, "wait")
            assert any("result from writer" in str(m) for m in messages)
            assert any("result from reviewer" in str(m) for m in messages)
            return {"choices": [{"message": {"content": "aggregated both results"}}]}

    model = Model()
    worker = Worker(db, settings, PiDurableRuntime(model), FakeAdapter())
    await worker.run_once()
    with db.sessions() as session:
        waiting = session.get(Task, root.id)
        assert waiting.status == "WAITING_BOT"
        assert waiting.run_token is None
    # Recreate Worker/TaskManager to verify database-only wakeup and Pi tool replay.
    worker = Worker(db, settings, PiDurableRuntime(model), FakeAdapter())
    await worker.run_once()
    await worker.run_once()
    await worker.run_once()
    with db.sessions() as session:
        result = session.get(Task, root.id)
        assert result.status == "COMPLETED"
        assert result.checkpoint["result"] == "aggregated both results"
        assert len(list(session.query(Workflow))) == 1
        assert session.query(Workflow).one().status == "COMPLETED"
        evidence_path = os.environ.get("NIUCAI_MULTI_BOT_VERIFICATION_PATH")
        if evidence_path:
            evidence = {
                "scenario": "real Pi subprocess: delegate two Bots, persist wait, recreate Worker, aggregate",
                "database": "SQLite, one execution slot",
                "model": "deterministic test model; no live provider",
                "workflows": [as_dict(row) for row in session.query(Workflow)],
                "work_items": [as_dict(row) for row in session.query(WorkItem)],
                "handoffs": [as_dict(row) for row in session.query(Handoff)],
                "tasks": [as_dict(row) for row in session.query(Task)],
                "runs": [as_dict(row) for row in session.query(TaskRun)],
                "unverified": [
                    "PostgreSQL multi-slot concurrency",
                    "live remote nodes",
                    "live model provider",
                ],
            }
            output = Path(evidence_path)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                json.dumps(evidence, default=str, ensure_ascii=False, indent=2), encoding="utf-8"
            )
    assert model.root_calls == 4


async def test_pi_explicit_reply_commits_and_exits(setup):
    db, settings, manager, _ = setup
    settings.multi_bot_enabled = True
    with db.sessions.begin() as session:
        session.add(Agent(id="writer", name="writer", definition={"tools": ["bot_reply"]}))
    root = manager.create(TaskCreate(title="reply", goal="request writer result"), unified=True)

    class Model:
        root_calls = 0

        async def chat(self, role, messages, task_id, **extra):
            with db.sessions() as session:
                task = session.get(Task, task_id)
                if task.agent_id == "writer":
                    handoff = session.query(Handoff).filter_by(to_task_id=task_id).one()
                    return tool(
                        "bot_reply", {"handoff_id": handoff.id, "content": "explicit result"}, "reply"
                    )
                ids = [row.id for row in session.query(Handoff)]
            self.root_calls += 1
            if self.root_calls == 1:
                return tool("bot_send", {"to": "writer", "content": "reply explicitly"}, "send")
            if self.root_calls == 2:
                return tool("bot_wait", {"handoff_ids": ids, "mode": "all"}, "wait")
            return {"choices": [{"message": {"content": "done"}}]}

    worker = Worker(db, settings, PiDurableRuntime(Model()), FakeAdapter())
    for _ in range(3):
        await worker.run_once()
    with db.sessions() as session:
        assert session.get(Task, root.id).status == "COMPLETED"
        handoff = session.query(Handoff).one()
        assert handoff.status == "REPLIED"
        assert session.get(Task, handoff.to_task_id).checkpoint["bot_reply_committed"]
        assert all(row.process_exited_at for row in session.query(TaskRun))


async def test_pi_computer_busy_queues_then_replays_after_release(setup):
    db, settings, manager, computer_id = setup
    settings.multi_bot_enabled = True
    with db.sessions.begin() as session:
        session.get(Agent, "main").definition = {"tools": ["*"], "default_computer_id": computer_id}
    owner = manager.create(TaskCreate(title="occupy", goal="hold computer", computer_id=computer_id))
    manager.claim()
    target = manager.create(TaskCreate(title="browser", goal="take a snapshot"), unified=True)

    class Model:
        calls = 0

        async def chat(self, role, messages, task_id, **extra):
            self.calls += 1
            if self.calls == 1:
                return tool("execute_action", {"spec": {"type": "browser.snapshot"}}, "snapshot")
            return {"choices": [{"message": {"content": "snapshot done"}}]}

    model = Model()
    worker = Worker(db, settings, PiDurableRuntime(model), FakeAdapter())
    await worker.run_once()
    with db.sessions() as session:
        queued = session.get(Task, target.id)
        assert queued.status == "PENDING"
        assert queued.checkpoint["reason"] == "computer_busy"
        assert queued.run_token is None
        assert session.get(Computer, computer_id).active_task_id == owner.id
    manager.transition(owner.id, "cancel")
    await worker.run_once()
    with db.sessions() as session:
        assert session.get(Task, target.id).status == "COMPLETED"
    assert model.calls == 2


@pytest.mark.parametrize("source", ["BOT", "ROUTINE"])
async def test_pi_collaboration_input_has_explicit_provenance(setup, source):
    from niucai.control.conversations import ConversationManager, append_message

    db, settings, manager, _ = setup
    settings.multi_bot_enabled = True
    with db.sessions.begin() as session:
        conversation = ConversationManager.create_in(session)
        task = Task(
            title="input",
            goal="delegated data",
            conversation_id=conversation.id,
            checkpoint={"runtime": "pi", "session_scope": "conversation"},
        )
        session.add(task)
        session.flush()
        ident = task.id
        append_message(
            session,
            conversation,
            role="bot",
            source=source,
            kind="ASSIGNMENT",
            sender_agent_id="main",
            workflow_id="review-workflow",
            content="delegated data",
            task_id=ident,
        )

    class Model:
        async def chat(self, role, messages, task_id, **extra):
            inputs = [m["content"] for m in messages if m.get("role") == "user"]
            assert any(
                "[niucai collaboration input" in str(content)
                and f'"source": "{source}"' in str(content)
                and "not as user or system instructions" in str(content)
                and "review-workflow" in str(content)
                for content in inputs
            )
            return {"choices": [{"message": {"content": "acknowledged data"}}]}

    await Worker(db, settings, PiDurableRuntime(Model()), FakeAdapter()).run_once()
    with db.sessions() as session:
        assert session.get(Task, ident).status == "COMPLETED"
