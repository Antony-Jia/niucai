from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from niucai.actions.adapters import FakeAdapter
from niucai.actions.gateway import ActionGateway
from niucai.api.app import create_app
from niucai.control.computers import ComputerManager
from niucai.control.progress import run_context, task_snapshot
from niucai.control.tasks import Conflict
from niucai.domain.schemas import TaskCreate
from niucai.storage.db import Action, Approval, Task, emit, now
from niucai.worker import Worker


def snapshot(db, task_id):
    with db.sessions() as s:
        return task_snapshot(s, s.get(Task, task_id))


def create(setup, computer=False):
    _, _, manager, cid = setup
    return manager.create(TaskCreate(title="progress", goal="test", computer_id=cid if computer else None))


def test_rest_event_projection_and_heartbeat_clock(setup):
    db, settings, manager, _ = setup
    task = create(setup)
    first = snapshot(db, task.id)["progress"]
    assert first["phase"] == "QUEUED" and first["wait_reason"] is None
    claimed = manager.claim()
    manager.phase(task.id, claimed.run_token, "MODEL_REQUEST")
    before = snapshot(db, task.id)["progress"]
    assert before["phase"] == "MODEL_REQUEST"
    assert manager.heartbeat(task.id, claimed.run_token)
    assert snapshot(db, task.id)["progress"] == before
    auth = {"Authorization": "Bearer " + settings.api_token.get_secret_value()}
    with TestClient(create_app(settings, db)) as client:
        assert client.get(f"/api/tasks/{task.id}", headers=auth).json()["progress"] == before
        assert client.get("/api/tasks", headers=auth).json()[0]["progress"] == before
        events = client.get("/api/events", headers=auth).json()
        assert [e for e in events if e["task_id"] == task.id][-1]["data"]["progress"] == before


@pytest.mark.parametrize("terminal", ["COMPLETED", "CANCELLED", "FAILED"])
def test_terminal_clears_stale_wait_but_preserves_session_and_result(setup, terminal):
    db, _, manager, _ = setup
    task = create(setup)
    claimed = manager.claim()
    manager.update(
        task.id,
        claimed.run_token,
        status=terminal,
        checkpoint={
            "reason": "WAITING_APPROVAL",
            "action_id": "old",
            "detail": "old wait",
            "pi_submission_id": "session",
            "result": "done",
        },
    )
    current = snapshot(db, task.id)
    assert "reason" not in current["checkpoint"] and "action_id" not in current["checkpoint"]
    assert current["checkpoint"]["pi_submission_id"] == "session"
    assert current["checkpoint"]["result"] == "done"
    assert current["progress"]["phase"] == terminal
    if terminal != "FAILED":
        assert current["progress"]["allowed_operations"] == []
        assert current["progress"]["wait_reason"] is None


def test_legacy_terminal_snapshot_does_not_expose_active_wait(setup):
    db, _, _, _ = setup
    task = create(setup)
    with db.sessions.begin() as s:
        t = s.get(Task, task.id)
        t.status = "COMPLETED"
        t.checkpoint = {"reason": "UNKNOWN", "action_id": "old"}
    assert "reason" not in snapshot(db, task.id)["checkpoint"]
    assert snapshot(db, task.id)["progress"]["wait_reason"] is None


def test_approval_cannot_be_bypassed_by_resume_and_clears_on_decision(setup):
    db, settings, manager, _ = setup
    task = create(setup)
    claimed = manager.claim()
    gateway = ActionGateway(db, settings, FakeAdapter())
    action = gateway.propose(
        task.id, claimed.run_token, {"type": "files.write", "path": "a", "content": "b"}, "approval"
    )
    manager.update(
        task.id,
        claimed.run_token,
        status="WAITING_HUMAN",
        checkpoint={"reason": "WAITING_APPROVAL", "action_id": action.id},
    )
    progress = snapshot(db, task.id)["progress"]
    assert progress["wait_reason"] == "APPROVAL_REQUIRED" and "resume" not in progress["allowed_operations"]
    with pytest.raises(Conflict):
        manager.transition(task.id, "resume")
    with db.sessions() as s:
        approval = s.scalar(select(Approval).where(Approval.action_id == action.id))
    gateway.decide(approval.id, False)
    current = snapshot(db, task.id)
    assert current["status"] == "PENDING"
    assert current["progress"]["wait_reason"] is None
    assert "reason" not in current["checkpoint"]


def test_unknown_blocks_resume_until_separate_reconciliation(setup):
    db, settings, manager, _ = setup
    task = create(setup)
    claimed = manager.claim()
    gateway = ActionGateway(db, settings, FakeAdapter())
    action = gateway.propose(task.id, claimed.run_token, {"type": "files.read", "path": "a"}, "unknown")
    with db.sessions.begin() as s:
        s.get(Action, action.id).status = "UNKNOWN"
    manager.update(
        task.id,
        claimed.run_token,
        status="WAITING_HUMAN",
        checkpoint={"reason": "UNKNOWN", "action_id": action.id},
    )
    assert snapshot(db, task.id)["progress"]["phase"] == "WAITING_RECONCILIATION"
    with pytest.raises(Conflict):
        manager.transition(task.id, "resume")
    manager.transition(task.id, "pause")
    with pytest.raises(Conflict):
        manager.transition(task.id, "resume")
    Worker(db, settings, None, FakeAdapter()).resolve_unknown(action.id, "SUCCEEDED")
    assert "resume" in snapshot(db, task.id)["progress"]["allowed_operations"]
    manager.transition(task.id, "resume")


def test_failure_retry_restores_approval_wait_without_claiming(setup):
    db, settings, manager, _ = setup
    task = create(setup)
    claimed = manager.claim()
    gateway = ActionGateway(db, settings, FakeAdapter())
    gateway.propose(
        task.id, claimed.run_token, {"type": "files.write", "path": "a", "content": "b"}, "retry-approval"
    )
    manager.update(
        task.id, claimed.run_token, status="FAILED", checkpoint={"error": "test", "detail": "failed"}
    )
    assert "retry" in snapshot(db, task.id)["progress"]["allowed_operations"]
    manager.transition(task.id, "retry")
    current = snapshot(db, task.id)
    assert current["status"] == "WAITING_HUMAN"
    assert current["progress"]["wait_reason"] == "APPROVAL_REQUIRED"
    assert manager.claim() is None


def test_approving_one_action_cannot_resume_past_another_unknown(setup):
    db, settings, manager, _ = setup
    task = create(setup)
    claimed = manager.claim()
    gateway = ActionGateway(db, settings, FakeAdapter())
    unknown = gateway.propose(task.id, claimed.run_token, {"type": "files.read", "path": "a"}, "first")
    action = gateway.propose(
        task.id, claimed.run_token, {"type": "files.write", "path": "a", "content": "b"}, "second"
    )
    with db.sessions.begin() as s:
        s.get(Action, unknown.id).status = "UNKNOWN"
    manager.update(
        task.id,
        claimed.run_token,
        status="WAITING_HUMAN",
        checkpoint={"reason": "UNKNOWN", "action_id": unknown.id},
    )
    with db.sessions() as s:
        approval = s.scalar(select(Approval).where(Approval.action_id == action.id))
    gateway.decide(approval.id, True)
    assert snapshot(db, task.id)["status"] == "WAITING_HUMAN"
    assert snapshot(db, task.id)["progress"]["wait_reason"] == "ACTION_UNKNOWN"
    assert manager.claim() is None


def test_approval_decided_before_wait_checkpoint_is_not_stranded(setup):
    db, settings, manager, _ = setup
    task = create(setup)
    claimed = manager.claim()
    gateway = ActionGateway(db, settings, FakeAdapter())
    action = gateway.propose(
        task.id, claimed.run_token, {"type": "files.write", "path": "a", "content": "b"}, "race"
    )
    with db.sessions() as s:
        approval = s.scalar(select(Approval).where(Approval.action_id == action.id))
    gateway.decide(approval.id, True)
    manager.update(
        task.id,
        claimed.run_token,
        status="WAITING_HUMAN",
        checkpoint={"reason": "WAITING_APPROVAL", "action_id": action.id},
    )
    assert snapshot(db, task.id)["status"] == "PENDING"
    assert snapshot(db, task.id)["progress"]["wait_reason"] is None


def test_retry_keeps_old_failure_as_history_not_current_error(setup):
    db, _, manager, _ = setup
    task = create(setup)
    claimed = manager.claim()
    manager.update(
        task.id, claimed.run_token, status="FAILED", checkpoint={"error": "old", "detail": "previous failure"}
    )
    manager.transition(task.id, "retry")
    checkpoint = snapshot(db, task.id)["checkpoint"]
    assert "error" not in checkpoint and "error_detail" not in checkpoint
    assert checkpoint["last_error"]["error"] == "old"


def test_missing_computer_and_control_have_distinct_recovery(setup):
    db, _, manager, cid = setup
    task = create(setup)
    claimed = manager.claim()
    manager.update(
        task.id, claimed.run_token, status="WAITING_HUMAN", checkpoint={"reason": "computer required"}
    )
    assert snapshot(db, task.id)["progress"]["wait_reason"] == "COMPUTER_REQUIRED"
    with pytest.raises(Conflict):
        manager.transition(task.id, "resume")
    manager.attach_computer(task.id, cid)
    ComputerManager(db).control(cid, "take-control")
    assert snapshot(db, task.id)["progress"]["wait_reason"] == "COMPUTER_HUMAN_CONTROL"
    with pytest.raises(Conflict):
        manager.transition(task.id, "resume")
    ComputerManager(db).control(cid, "hand-back")
    manager.transition(task.id, "resume")
    assert snapshot(db, task.id)["progress"]["phase"] == "QUEUED"


def test_computer_queue_and_recovery_progress(setup):
    db, _, manager, _ = setup
    first = create(setup, computer=True)
    second = create(setup, computer=True)
    claimed = manager.claim()
    assert claimed.id == first.id
    assert snapshot(db, second.id)["progress"]["wait_reason"] == "COMPUTER_BUSY"
    with db.sessions.begin() as s:
        s.get(Task, first.id).lease_until = now() - timedelta(seconds=1)
    recovered = manager.claim()
    assert recovered.id == first.id
    assert snapshot(db, first.id)["progress"]["phase"] == "RECOVERING"
    with pytest.raises(Conflict):
        manager.phase(first.id, claimed.run_token, "MODEL_REQUEST")


def test_late_model_events_do_not_advance_new_run_or_pause(setup):
    db, _, manager, _ = setup
    task = create(setup)
    old = manager.claim()
    context = run_context.set((task.id, old.run_token))
    try:
        with db.sessions.begin() as s:
            emit(s, "model.requested", task.id, role="executor")
        assert snapshot(db, task.id)["progress"]["phase"] == "MODEL_REQUEST"
        manager.transition(task.id, "pause")
        before = snapshot(db, task.id)["progress"]
        with db.sessions.begin() as s:
            emit(s, "model.completed", task.id)
        assert snapshot(db, task.id)["progress"] == before
        manager.transition(task.id, "resume")
        manager.claim()
        before = snapshot(db, task.id)["progress"]
        with db.sessions.begin() as s:
            emit(s, "model.completed", task.id)
        assert snapshot(db, task.id)["progress"] == before
    finally:
        run_context.reset(context)


@pytest.mark.parametrize(
    "reason,code",
    [
        ("step budget exhausted", "STEP_BUDGET_EXHAUSTED"),
        ("model turn budget exhausted", "MODEL_BUDGET_EXHAUSTED"),
        ("graph budget exhausted", "GRAPH_BUDGET_EXHAUSTED"),
        ("agent requested human", "HUMAN_REQUESTED"),
    ],
)
def test_budget_and_human_waits_remain_resumable(setup, reason, code):
    db, settings, manager, _ = setup
    task = create(setup)
    claimed = manager.claim()
    manager.update(
        task.id,
        claimed.run_token,
        status="WAITING_HUMAN",
        current_step=settings.max_steps,
        checkpoint={"reason": reason, "pi_human_wait": "tool"},
    )
    assert snapshot(db, task.id)["progress"]["wait_reason"] == code
    manager.transition(task.id, "resume")
    checkpoint = snapshot(db, task.id)["checkpoint"]
    assert "reason" not in checkpoint
    if reason == "step budget exhausted":
        assert checkpoint["budget_limit"] == settings.max_steps * 2
    if reason == "agent requested human":
        assert checkpoint["pi_human_resumed"] == "tool"
