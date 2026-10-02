from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select

from niucai.actions.adapters import FakeAdapter, LocalAdapter
from niucai.actions.gateway import ActionGateway
from niucai.api.app import create_app
from niucai.context.compiler import ContextCompiler
from niucai.control.computers import ComputerManager
from niucai.control.tasks import Conflict
from niucai.domain.schemas import Decision, Plan, TaskCreate
from niucai.storage.db import Action, Approval, Audit, Event, Memory, Task, TaskRun, now
from niucai.worker import Worker


def created(setup, computer=True):
    db, settings, manager, computer_id = setup
    return manager.create(
        TaskCreate(title="test", goal="browse example", computer_id=computer_id if computer else None)
    )


class ScriptRuntime:
    def __init__(self, decisions):
        self.decisions = iter(decisions)
        self.calls = 0

    async def plan(self, package, task_id):
        return Plan(goal=package["goal"], steps=[])

    async def decide(self, package, task_id):
        self.calls += 1
        return Decision.model_validate(next(self.decisions)).validate_action()


async def test_worker_end_to_end(setup):
    db, settings, manager, _ = setup
    task = created(setup)
    runtime = ScriptRuntime(
        [
            {"kind": "action", "explanation": "observe", "action": {"type": "browser.snapshot"}},
            {"kind": "complete", "explanation": "done"},
        ]
    )
    worker = Worker(db, settings, runtime, FakeAdapter())
    assert await worker.run_once()
    with db.sessions() as s:
        task = s.get(Task, task.id)
        assert task.status == "COMPLETED"
        assert task.current_step == 1
        assert task.checkpoint["result"] == "done"
        assert "proposal" not in task.checkpoint
        assert s.scalar(select(Action)).status == "SUCCEEDED"
        assert s.scalar(select(TaskRun)).status == "COMPLETED"
        assert "task.completed" in s.scalars(select(Event.type)).all()


async def test_approval_resume_without_new_decision(setup):
    db, settings, manager, _ = setup
    task = created(setup, computer=False)
    runtime = ScriptRuntime(
        [
            {
                "kind": "action",
                "explanation": "write",
                "action": {"type": "files.write", "path": "a.txt", "content": "hello"},
            },
            {"kind": "complete", "explanation": "done"},
        ]
    )
    worker = Worker(db, settings, runtime, FakeAdapter())
    await worker.run_once()
    with db.sessions() as s:
        assert s.get(Task, task.id).status == "WAITING_HUMAN"
        approval = s.scalar(select(Approval))
        action_id = approval.action_id
    worker.actions.decide(approval.id, True)
    await worker.run_once()
    with db.sessions() as s:
        assert s.get(Task, task.id).status == "COMPLETED"
        assert s.get(Action, action_id).status == "SUCCEEDED"
        assert len(s.scalars(select(Action)).all()) == 1
    assert runtime.calls == 2


async def test_denied_action_not_executed(setup):
    db, settings, manager, _ = setup
    created(setup, computer=False)
    runtime = ScriptRuntime(
        [
            {
                "kind": "action",
                "explanation": "write",
                "action": {"type": "files.write", "path": "a", "content": "b"},
            },
            {"kind": "complete", "explanation": "declined"},
        ]
    )
    worker = Worker(db, settings, runtime, FakeAdapter())
    await worker.run_once()
    with db.sessions() as s:
        approval = s.scalar(select(Approval))
    worker.actions.decide(approval.id, False)
    await worker.run_once()
    with db.sessions() as s:
        assert s.scalar(select(Action)).status == "DENIED"


def test_takeover_invalidates_worker_and_handback_resumes(setup):
    db, settings, manager, computer_id = setup
    task = created(setup)
    claimed = manager.claim()
    computers = ComputerManager(db)
    assert computers.control(computer_id, "take-control").control == "HUMAN"
    with pytest.raises(Conflict):
        manager.update(task.id, claimed.run_token, status="COMPLETED")
    assert manager.claim() is None
    computers.control(computer_id, "hand-back")
    assert manager.claim().id == task.id


def test_manual_pause_not_auto_resumed_by_handback(setup):
    db, settings, manager, computer_id = setup
    task = created(setup)
    manager.transition(task.id, "pause")
    ComputerManager(db).control(computer_id, "take-control")
    ComputerManager(db).control(computer_id, "hand-back")
    with db.sessions() as s:
        assert s.get(Task, task.id).status == "PAUSED"


def test_expired_lease_recovery_and_fencing(setup):
    db, settings, manager, _ = setup
    task = created(setup)
    old = manager.claim()
    with db.sessions.begin() as s:
        s.get(Task, task.id).lease_until = now() - timedelta(seconds=1)
    recovered = manager.claim()
    assert recovered.id == old.id and recovered.run_token != old.run_token
    with pytest.raises(Conflict):
        manager.update(task.id, old.run_token, status="COMPLETED")
    assert not manager.heartbeat(task.id, old.run_token)


async def test_interrupted_action_never_replayed(setup):
    db, settings, manager, _ = setup
    task = created(setup)
    task = manager.claim()
    gateway = ActionGateway(db, settings, FakeAdapter())
    action = gateway.propose(task.id, task.run_token, {"type": "browser.snapshot"}, "test")
    with db.sessions.begin() as s:
        s.get(Action, action.id).status = "EXECUTING"
    result = await gateway.execute(action.id, task.run_token)
    assert result.status == "UNKNOWN"
    assert (await gateway.execute(action.id, task.run_token)).status == "UNKNOWN"
    Worker(db, settings, None, FakeAdapter()).resolve_unknown(action.id, "FAILED")
    with db.sessions() as s:
        assert s.get(Action, action.id).result["human_reconciled"]
        assert len(s.scalars(select(Audit)).all()) == 3


async def test_idempotency_and_stale_refs(setup):
    db, settings, manager, _ = setup
    created(setup)
    task = manager.claim()
    gateway = ActionGateway(db, settings, FakeAdapter())
    spec = {"type": "browser.snapshot"}
    action = gateway.propose(task.id, task.run_token, spec, "same")
    assert gateway.propose(task.id, task.run_token, spec, "same").id == action.id
    with pytest.raises(Conflict):
        gateway.propose(task.id, task.run_token, {"type": "browser.screenshot"}, "same")
    result = await gateway.execute(action.id, task.run_token)
    assert result.status == "SUCCEEDED"
    assert (await gateway.execute(action.id, task.run_token)).result == result.result
    click = gateway.propose(task.id, task.run_token, {"type": "browser.click", "ref": "e999"}, "click")
    with db.sessions() as s:
        approval = s.scalar(select(Approval).where(Approval.action_id == click.id))
    gateway.decide(approval.id, True)
    assert (await gateway.execute(click.id, task.run_token)).status == "FAILED"


def test_single_computer_lease(setup):
    created(setup)
    created(setup)
    db, settings, manager, _ = setup
    assert manager.claim()
    assert manager.claim() is None


def test_cancellation_blocks_actions(setup):
    db, settings, manager, _ = setup
    created(setup)
    task = manager.claim()
    manager.transition(task.id, "cancel")
    with pytest.raises(Conflict):
        ActionGateway(db, settings, FakeAdapter()).propose(
            task.id, task.run_token, {"type": "browser.snapshot"}, "cancelled"
        )


def test_strict_action_schema():
    with pytest.raises(ValidationError):
        Decision.model_validate(
            {
                "kind": "action",
                "explanation": "bad",
                "action": {"type": "shell.exec", "argv": ["ls"], "command": "rm -rf /"},
            }
        )
    with pytest.raises(ValueError):
        Decision(kind="action", explanation="bad").validate_action()


def test_context_selection(setup):
    db, settings, _, _ = setup
    task = created(setup)
    with db.sessions.begin() as s:
        s.add(Memory(kind="semantic", content="example domain", tags=[]))
        s.add(Memory(kind="semantic", content="unrelated apples", tags=[]))
    context = ContextCompiler(db, settings).compile(task.id)
    assert context["memories"][0]["content"] == "example domain"
    assert len(context["memories"]) == 1
    assert "Kernel owns" in context["policies"]


async def test_workspace_containment_and_file_read_limit(setup):
    db, settings, _, _ = setup
    adapter = LocalAdapter(settings)
    for path in ["../escape", "/etc/passwd", "."]:
        with pytest.raises(ValueError):
            adapter.path(path)
    outside = settings.workspace.parent / "outside"
    outside.mkdir()
    (settings.workspace / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        adapter.path("link/file")
    await adapter.execute({"type": "files.write", "path": "safe/a", "content": "hello"}, None)
    assert (await adapter.execute({"type": "files.read", "path": "safe/a"}, None))["content"] == "hello"


def test_rest_auth_and_persistence(setup):
    db, settings, manager, _ = setup
    headers = {"Authorization": "Bearer " + "x" * 40}
    with TestClient(create_app(settings, db)) as client:
        assert client.get("/api/tasks").status_code == 401
        response = client.post("/api/tasks", json={"title": "api", "goal": "test"}, headers=headers)
        assert response.status_code == 201
        task_id = response.json()["id"]
        assert client.post(f"/api/tasks/{task_id}/pause", headers=headers).json()["status"] == "PAUSED"
        assert client.post(f"/api/tasks/{task_id}/resume", headers=headers).json()["status"] == "PENDING"
        assert client.get(f"/api/tasks/{task_id}", headers=headers).status_code == 200
        assert client.get("/api/tasks/missing", headers=headers).status_code == 404
        assert client.get("/api/events", headers=headers).json()


def test_websocket_auth_and_replay(setup):
    db, settings, _, _ = setup
    created(setup)
    with TestClient(create_app(settings, db)) as client:
        with client.websocket_connect("/api/events") as ws:
            ws.send_json({"token": "x" * 40, "after": 0})
            assert ws.receive_json()["type"] == "connected"
            event = ws.receive_json()
            assert event["type"] == "task.created"
            assert event["id"] > 0
        from starlette.websockets import WebSocketDisconnect

        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/api/events") as ws:
                ws.send_json({"token": "wrong"})
                ws.receive_json()


async def test_restart_consumes_successful_action_without_reexecuting(setup):
    db, settings, manager, _ = setup
    created(setup)
    task = manager.claim()
    proposal = {"kind": "action", "explanation": "observe", "action": {"type": "browser.snapshot"}}
    manager.update(
        task.id, task.run_token, plan={"goal": "test", "steps": []}, checkpoint={"proposal": proposal}
    )
    gateway = ActionGateway(db, settings, FakeAdapter())
    action = gateway.propose(task.id, task.run_token, proposal["action"], f"{task.id}:0")
    await gateway.execute(action.id, task.run_token)
    # Crash after side effect/result commit, before Worker checkpoint advances.
    with db.sessions.begin() as s:
        s.get(Task, task.id).lease_until = now() - timedelta(seconds=1)

    class NoExecute:
        async def execute(self, spec, computer):
            raise AssertionError("successful action must not execute twice")

    runtime = ScriptRuntime([{"kind": "complete", "explanation": "recovered"}])
    await Worker(db, settings, runtime, NoExecute()).run_once()
    with db.sessions() as s:
        assert s.get(Task, task.id).status == "COMPLETED"
        assert s.get(Task, task.id).retry_count == 1
        assert len(s.scalars(select(Action)).all()) == 1


async def test_pause_while_model_returns_blocks_proposal(setup):
    db, settings, manager, _ = setup
    task = created(setup)

    class PausingRuntime(ScriptRuntime):
        async def decide(self, package, task_id):
            manager.transition(task_id, "pause")
            return Decision.model_validate(
                {"kind": "action", "explanation": "late", "action": {"type": "browser.snapshot"}}
            )

    await Worker(db, settings, PausingRuntime([]), FakeAdapter()).run_once()
    with db.sessions() as s:
        assert s.get(Task, task.id).status == "PAUSED"
        assert not s.scalars(select(Action)).all()


async def test_executor_error_after_dispatch_is_unknown(setup):
    db, settings, manager, _ = setup
    created(setup)
    task = manager.claim()

    class UncertainAdapter:
        async def execute(self, spec, computer):
            raise OSError("lost connection after possible side effect")

    gateway = ActionGateway(db, settings, UncertainAdapter())
    action = gateway.propose(
        task.id, task.run_token, {"type": "browser.navigate", "url": "https://example.com"}, "error"
    )
    assert (await gateway.execute(action.id, task.run_token)).status == "UNKNOWN"


def test_retry_failed_and_auth_required_on_startup(setup):
    db, settings, manager, _ = setup
    created(setup)
    task = manager.claim()
    manager.update(task.id, task.run_token, status="FAILED")
    assert manager.transition(task.id, "retry").status == "PENDING"
    assert manager.claim().retry_count == 1
    settings.api_token = ""
    with pytest.raises(RuntimeError, match="NIUCAI_API_TOKEN"):
        with TestClient(create_app(settings, db)):
            pass


def test_context_budget_prunes_large_plan(setup):
    db, settings, manager, _ = setup
    task = created(setup)
    with db.sessions.begin() as s:
        s.get(Task, task.id).plan = {
            "goal": "test",
            "steps": [{"description": "x" * 1000} for _ in range(30)],
        }
    import json

    context = ContextCompiler(db, settings).compile(task.id)
    assert len(json.dumps(context, ensure_ascii=False)) <= settings.context_chars
    assert context["plan"]["truncated"]
