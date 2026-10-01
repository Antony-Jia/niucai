"""Exercise actual DeepAgents/LangGraph with deterministic model and computer I/O."""

import json

import pytest
from sqlalchemy import select

from niucai.actions.adapters import FakeAdapter
from niucai.agents.deepagent_adapter import DeepAgentRuntime
from niucai.control.computers import ComputerManager
from niucai.domain.schemas import TaskCreate
from niucai.storage.db import Action, Approval, Database, Task
from niucai.worker import Worker

pytest.importorskip("deepagents")


def call(ident, spec):
    return {
        "content": "",
        "tool_calls": [
            {
                "id": ident,
                "type": "function",
                "function": {
                    "name": "execute_action",
                    "arguments": json.dumps({"spec": spec}),
                },
            }
        ],
    }


class Model:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.messages = []

    async def structured(self, role, package, schema, task_id):
        return schema(goal=package["goal"], steps=[])

    async def chat(self, role, messages, task_id, **extra):
        self.messages.append(messages)
        response = self.responses.pop(0)
        if callable(response):
            response = response(messages)
        return {"choices": [{"message": response}]}


class Computer(FakeAdapter):
    def __init__(self):
        self.calls = []

    async def execute(self, spec, computer):
        self.calls.append(spec)
        return await super().execute(spec, computer)


async def run(db, settings, manager, model, computer):
    worker = Worker(db, settings, DeepAgentRuntime(model), computer)
    assert await worker.run_once()


def state(db, ident):
    with db.sessions() as s:
        return s.get(Task, ident)


async def test_feedback_drives_correction_in_same_session(setup):
    db, settings, manager, cid = setup
    task = manager.create(TaskCreate(title="test", goal="observe and fix", computer_id=cid))

    def corrected(messages):
        feedback = [m for m in messages if m["role"] == "tool"]
        assert '"status": "FAILED"' in feedback[-1]["content"]
        return call("snapshot", {"type": "browser.snapshot"})

    def final(messages):
        assert '"observation"' in [m for m in messages if m["role"] == "tool"][-1]["content"]
        return {"content": "Observed successfully."}

    model = Model(call("forbidden", {"type": "shell.exec", "argv": ["whoami"]}), corrected, final)
    computer = Computer()
    await run(db, settings, manager, model, computer)
    assert state(db, task.id).status == "COMPLETED"
    assert len(model.messages) == 3
    assert len(computer.calls) == 1
    assert state(db, task.id).current_step == 1


@pytest.mark.parametrize("approved", [True, False])
async def test_approval_survives_restart_without_reproposal(setup, approved):
    db, settings, manager, cid = setup
    task = manager.create(TaskCreate(title="test", goal="write report", computer_id=cid))
    model = Model(
        call("write", {"type": "files.write", "path": "report.txt", "content": "hello"}),
        {"content": "Finished after approval feedback."},
    )
    computer = Computer()
    await run(db, settings, manager, model, computer)
    assert state(db, task.id).status == "WAITING_HUMAN"
    assert len(model.messages) == 1
    assert not computer.calls
    # Reconstruct every runtime object and database engine, as after a server restart.
    restarted = Database(settings.database_url)
    try:
        with restarted.sessions() as s:
            approval = s.scalar(select(Approval))
        worker = Worker(restarted, settings, DeepAgentRuntime(model), computer)
        worker.actions.decide(approval.id, approved)
        assert await worker.run_once()
        assert state(restarted, task.id).status == "COMPLETED"
        assert len(computer.calls) == int(approved)
        with restarted.sessions() as s:
            actions = s.scalars(select(Action)).all()
        assert len(actions) == 1
        assert actions[0].status == ("SUCCEEDED" if approved else "DENIED")
        assert len(model.messages) == 2
    finally:
        restarted.engine.dispose()


async def test_resume_does_not_bypass_pending_approval(setup):
    db, settings, manager, cid = setup
    task = manager.create(TaskCreate(title="test", goal="write", computer_id=cid))
    model = Model(call("write", {"type": "files.write", "path": "a", "content": "x"}))
    computer = Computer()
    await run(db, settings, manager, model, computer)
    manager.transition(task.id, "resume")
    await run(db, settings, manager, model, computer)
    assert state(db, task.id).status == "WAITING_HUMAN"
    assert len(model.messages) == 1
    assert not computer.calls


async def test_takeover_during_model_call_preserves_session(setup):
    db, settings, manager, cid = setup
    task = manager.create(TaskCreate(title="test", goal="observe", computer_id=cid))

    def takeover(messages):
        ComputerManager(db).control(cid, "take-control")
        return call("lost", {"type": "browser.snapshot"})

    model = Model(takeover, call("restored", {"type": "browser.snapshot"}), {"content": "done"})
    computer = Computer()
    await run(db, settings, manager, model, computer)
    assert state(db, task.id).status == "PAUSED"
    assert not computer.calls
    ComputerManager(db).control(cid, "hand-back")
    await run(db, settings, manager, model, computer)
    assert state(db, task.id).status == "COMPLETED"
    assert len(computer.calls) == 1


async def test_step_budget_interrupt_resumes_pending_tool(setup):
    db, settings, manager, cid = setup
    settings.max_steps = 1
    task = manager.create(TaskCreate(title="test", goal="observe twice", computer_id=cid))
    model = Model(
        call("one", {"type": "browser.snapshot"}),
        call("two", {"type": "browser.snapshot"}),
        {"content": "done"},
    )
    computer = Computer()
    await run(db, settings, manager, model, computer)
    assert state(db, task.id).status == "WAITING_HUMAN"
    assert len(computer.calls) == 1
    manager.transition(task.id, "resume")
    await run(db, settings, manager, model, computer)
    assert state(db, task.id).status == "COMPLETED"
    assert len(computer.calls) == 2
    assert state(db, task.id).current_step == 2


async def test_delegated_tool_approval_keeps_child_checkpoint(setup):
    db, settings, manager, cid = setup
    task = manager.create(TaskCreate(title="test", goal="delegate report", computer_id=cid))
    delegate = {
        "content": "",
        "tool_calls": [
            {
                "id": "delegate",
                "type": "function",
                "function": {
                    "name": "task",
                    "arguments": json.dumps(
                        {"description": "write report", "subagent_type": "general-purpose"}
                    ),
                },
            }
        ],
    }
    model = Model(
        delegate,
        call("child-write", {"type": "files.write", "path": "a", "content": "x"}),
        {"content": "child done"},
        {"content": "main done"},
    )
    computer = Computer()
    await run(db, settings, manager, model, computer)
    assert state(db, task.id).status == "WAITING_HUMAN"
    assert len(model.messages) == 2
    with db.sessions() as s:
        approval = s.scalar(select(Approval))
    worker = Worker(db, settings, DeepAgentRuntime(model), computer)
    worker.actions.decide(approval.id, True)
    assert await worker.run_once()
    assert state(db, task.id).status == "COMPLETED"
    assert len(computer.calls) == 1
    assert len(model.messages) == 4
    with db.sessions() as s:
        assert len(s.scalars(select(Action)).all()) == 1


async def test_unknown_outcome_blocks_model_until_reconciled(setup):
    db, settings, manager, cid = setup
    task = manager.create(TaskCreate(title="test", goal="navigate", computer_id=cid))

    class TimeoutComputer(Computer):
        async def execute(self, spec, computer):
            self.calls.append(spec)
            raise TimeoutError()

    model = Model(
        call("navigate", {"type": "browser.navigate", "url": "https://example.com"}),
        {"content": "Recorded human-inspected outcome."},
    )
    computer = TimeoutComputer()
    await run(db, settings, manager, model, computer)
    assert state(db, task.id).status == "WAITING_HUMAN"
    assert len(model.messages) == 1
    manager.transition(task.id, "resume")
    await run(db, settings, manager, model, computer)
    assert state(db, task.id).status == "WAITING_HUMAN"
    assert len(computer.calls) == 1
    assert len(model.messages) == 1
    worker = Worker(db, settings, DeepAgentRuntime(model), computer)
    worker.resolve_unknown(state(db, task.id).checkpoint["action_id"], "SUCCEEDED")
    manager.transition(task.id, "resume")
    assert await worker.run_once()
    assert state(db, task.id).status == "COMPLETED"
    assert len(computer.calls) == 1


async def test_crash_after_side_effect_replays_journal_without_execution(setup, monkeypatch):
    db, settings, manager, cid = setup
    task = manager.create(TaskCreate(title="test", goal="observe", computer_id=cid))
    model = Model(call("once", {"type": "browser.snapshot"}), {"content": "done"})
    computer = Computer()
    worker = Worker(db, settings, DeepAgentRuntime(model), computer)
    original = worker.tasks.update

    class ProcessCrash(BaseException):
        pass

    def crash(task_id, token, **changes):
        if "current_step" in changes:
            raise ProcessCrash()
        return original(task_id, token, **changes)

    monkeypatch.setattr(worker.tasks, "update", crash)
    with pytest.raises(ProcessCrash):
        await worker.run_once()
    assert len(computer.calls) == 1
    manager.transition(task.id, "pause")
    manager.transition(task.id, "resume")
    await run(db, settings, manager, model, computer)
    assert state(db, task.id).status == "COMPLETED"
    assert state(db, task.id).current_step == 1
    assert len(computer.calls) == 1


async def test_persisted_final_answer_recovers_without_model_call(setup, monkeypatch):
    db, settings, manager, cid = setup
    task = manager.create(TaskCreate(title="test", goal="answer", computer_id=cid))
    model = Model({"content": "durable answer"})
    computer = Computer()
    worker = Worker(db, settings, DeepAgentRuntime(model), computer)
    original = worker.tasks.update

    class ProcessCrash(BaseException):
        pass

    def crash(task_id, token, **changes):
        if changes.get("status") == "COMPLETED":
            raise ProcessCrash()
        return original(task_id, token, **changes)

    monkeypatch.setattr(worker.tasks, "update", crash)
    with pytest.raises(ProcessCrash):
        await worker.run_once()
    manager.transition(task.id, "pause")
    manager.transition(task.id, "resume")
    await run(db, settings, manager, model, computer)
    assert state(db, task.id).status == "COMPLETED"
    assert state(db, task.id).checkpoint["result"] == "durable answer"
    assert len(model.messages) == 1


async def test_old_worker_cannot_write_graph_checkpoint(setup):
    from niucai.agents.checkpointer import DatabaseSaver
    from niucai.control.tasks import Conflict

    db, settings, manager, cid = setup
    task = manager.create(TaskCreate(title="test", goal="pause", computer_id=cid))
    claimed = manager.claim()
    saver = DatabaseSaver(db, task.id, claimed.run_token)
    manager.transition(task.id, "pause")
    with pytest.raises(Conflict):
        await saver.aput({"configurable": {"thread_id": task.id}}, {"id": "stale"}, {}, {})


async def test_reused_provider_call_id_does_not_collapse_distinct_turns(setup):
    db, settings, manager, cid = setup
    task = manager.create(TaskCreate(title="test", goal="observe twice", computer_id=cid))
    model = Model(
        call("call_0", {"type": "browser.snapshot"}),
        call("call_0", {"type": "browser.snapshot"}),
        {"content": "done"},
    )
    computer = Computer()
    await run(db, settings, manager, model, computer)
    assert state(db, task.id).status == "COMPLETED"
    assert len(computer.calls) == 2
    assert state(db, task.id).current_step == 2


async def test_stale_locator_feedback_triggers_fresh_observation(setup):
    db, settings, manager, cid = setup
    task = manager.create(TaskCreate(title="test", goal="click", computer_id=cid))

    def correct(messages):
        feedback = json.loads([m for m in messages if m["role"] == "tool"][-1]["content"])
        assert feedback["status"] == "FAILED"
        assert "stale browser reference" in feedback["result"]["detail"]
        return call("refresh", {"type": "browser.snapshot"})

    model = Model(
        call("observe", {"type": "browser.snapshot"}),
        call("click", {"type": "browser.click", "ref": "e99"}),
        correct,
        {"content": "refreshed"},
    )
    computer = Computer()
    await run(db, settings, manager, model, computer)
    with db.sessions() as s:
        approval = s.scalar(select(Approval))
    worker = Worker(db, settings, DeepAgentRuntime(model), computer)
    worker.actions.decide(approval.id, True)
    assert await worker.run_once()
    assert state(db, task.id).status == "COMPLETED"
    assert [c["type"] for c in computer.calls] == ["browser.snapshot", "browser.snapshot"]


async def test_plan_revision_and_human_wait_resume_in_session(setup):
    db, settings, manager, cid = setup
    task = manager.create(TaskCreate(title="test", goal="revise", computer_id=cid))

    def named(ident, name, args):
        return {
            "content": "",
            "tool_calls": [
                {
                    "id": ident,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(args),
                    },
                }
            ],
        }

    plan = {"goal": "revise", "steps": [{"id": "1", "type": "browser", "description": "manual login"}]}
    model = Model(
        named("plan", "update_plan", {"plan": plan}),
        named("human", "wait_for_human", {"reason": "Please log in."}),
        {"content": "resumed"},
    )
    computer = Computer()
    await run(db, settings, manager, model, computer)
    assert state(db, task.id).status == "WAITING_HUMAN"
    assert state(db, task.id).plan["steps"][0]["description"] == "manual login"
    manager.transition(task.id, "resume")
    await run(db, settings, manager, model, computer)
    assert state(db, task.id).status == "COMPLETED"
    assert len(model.messages) == 3


@pytest.mark.parametrize("error", [OSError("snapshot connection failed"), TimeoutError()])
async def test_read_only_executor_failure_returns_feedback_without_human_wait(setup, error):
    db, settings, manager, cid = setup
    task = manager.create(TaskCreate(title="test", goal="observe after transient failure", computer_id=cid))

    class FlakyComputer(Computer):
        async def execute(self, spec, computer):
            if not self.calls:
                self.calls.append(spec)
                raise error
            return await super().execute(spec, computer)

    def retry(messages):
        feedback = json.loads([m for m in messages if m["role"] == "tool"][-1]["content"])
        assert feedback["status"] == "FAILED"
        return call("retry", {"type": "browser.snapshot"})

    model = Model(call("first", {"type": "browser.snapshot"}), retry, {"content": "observed"})
    computer = FlakyComputer()
    await run(db, settings, manager, model, computer)
    assert state(db, task.id).status == "COMPLETED"
    assert len(computer.calls) == 2
    with db.sessions() as s:
        assert not s.scalars(select(Approval)).all()
