from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from niucai.api.app import create_app
from niucai.control.remote import RemoteManager
from niucai.control.tasks import Conflict
from niucai.domain.schemas import TaskCreate
from niucai.storage.db import RemoteJob, now


def remote(setup):
    db, settings, *_ = setup
    settings.remote_pi_enabled = True
    return RemoteManager(db, settings)


def node(manager):
    value = manager.create_node("Pi node")
    manager.node_heartbeat(value["id"], {"executor": "pi", "available": True})
    return value


def job(manager, key, allowed=()):
    value = manager.submit("Produce a bounded report", key, allowed)
    manager.control(value["id"], "approve")
    return value


def test_queue_capacity_affinity_and_idempotency(setup):
    manager = remote(setup)
    a, b = node(manager), node(manager)
    first = job(manager, "one", [a["id"]])
    second = job(manager, "two")
    third = job(manager, "three")
    claimed = manager.claim(b["id"])
    assert claimed["id"] == second["id"]
    assert claimed["artifact_transport"] is False  # standalone jobs return inline files
    assert manager.claim(a["id"])["id"] == first["id"]
    assert manager.claim(a["id"]) is None
    assert manager.claim(b["id"]) is None
    assert manager.get(third["id"])["status"] == "QUEUED"
    assert manager.submit("Produce a bounded report", "one", [a["id"]])["id"] == first["id"]
    with pytest.raises(Conflict):
        manager.submit("different prompt", "one")


def test_lease_loss_does_not_release_or_accept_late_success(setup):
    manager = remote(setup)
    a = node(manager)
    value = job(manager, "one")
    claimed = manager.claim(a["id"])
    with setup[0].sessions.begin() as s:
        s.get(RemoteJob, value["id"]).lease_until = now() - timedelta(seconds=1)
    assert manager.get(value["id"])["status"] == "LOST"
    with pytest.raises(Conflict):
        manager.control(value["id"], "retry")
    job(manager, "two")
    assert manager.claim(a["id"]) is None
    assert manager.heartbeat(a["id"], value["id"], claimed["attempt_id"])["stop"]
    result = manager.finish(a["id"], value["id"], claimed["attempt_id"], "COMPLETED", {"answer": "late"})
    assert result["status"] == "FAILED" and "answer" not in result["result"]
    assert manager.claim(a["id"]) is not None


def test_cancellation_ack_retry_fencing_and_event_dedup(setup):
    manager = remote(setup)
    a, b = node(manager), node(manager)
    value = job(manager, "one")
    claimed = manager.claim(a["id"])
    attempt = claimed["attempt_id"]
    with pytest.raises(Conflict):
        manager.heartbeat(b["id"], value["id"], attempt)
    assert manager.event(a["id"], value["id"], attempt, 1, "session", {"session_id": "pi-session"})[
        "accepted"
    ]
    assert not manager.event(a["id"], value["id"], attempt, 1, "session", {})["accepted"]
    with pytest.raises(Conflict):
        manager.event(a["id"], value["id"], attempt, 3, "assistant", {})
    manager.control(value["id"], "cancel")
    assert manager.claim(a["id"]) is None
    manager.finish(a["id"], value["id"], attempt, "COMPLETED", {"answer": "late"})
    manager.control(value["id"], "retry")
    manager.control(value["id"], "approve")
    assert manager.claim(b["id"]) is None  # persistent session affinity
    new = manager.claim(a["id"])
    assert new["attempt_id"] != attempt
    with pytest.raises(Conflict):
        manager.finish(a["id"], value["id"], attempt, "COMPLETED", {})


def test_parent_pause_stops_child_and_pending_approval(setup):
    manager = remote(setup)
    a = node(manager)
    tasks = setup[2]
    parent = tasks.create(TaskCreate(title="parent", goal="delegate"))
    owned = tasks.claim()
    value = manager.submit("child", "child", parent_task_id=parent.id, parent_token=owned.run_token)
    manager.control(value["id"], "approve")
    claimed = manager.claim(a["id"])
    tasks.transition(parent.id, "pause")
    assert manager.get(value["id"])["status"] == "CANCELLING"
    assert manager.heartbeat(a["id"], value["id"], claimed["attempt_id"])["stop"]


def test_node_credentials_cannot_control_kernel_or_other_nodes(setup):
    manager = remote(setup)
    a, b = node(manager), node(manager)
    with TestClient(create_app(setup[1], setup[0])) as client:
        credential = {"Authorization": f"Bearer {a['token']}"}
        assert client.get("/api/tasks", headers=credential).status_code == 401
        assert client.post(f"/api/remote/runner/{b['id']}/claim", headers=credential).status_code == 401
        assert client.post(f"/api/remote/runner/{a['id']}/claim", headers=credential).status_code == 200
        admin = {"Authorization": "Bearer " + "x" * 40}
        nodes = client.get("/api/remote/nodes", headers=admin).json()
        assert "token_hash" not in nodes[0] and "token" not in nodes[0]
        value = client.post(
            "/api/remote/jobs", headers=admin, json={"prompt": "work", "idempotency_key": "api"}
        ).json()
        assert value["status"] == "WAITING_APPROVAL"
        assert manager.claim(a["id"]) is None


def test_resuming_parent_does_not_revoke_independent_child_lease(setup):
    manager = remote(setup)
    a = node(manager)
    tasks = setup[2]
    parent = tasks.create(TaskCreate(title="parent", goal="delegate"))
    owned = tasks.claim()
    value = manager.submit("child", "resume-child", parent_task_id=parent.id, parent_token=owned.run_token)
    tasks.phase(parent.id, owned.run_token, "WAITING_REMOTE")
    from niucai.control.progress import task_progress
    from niucai.storage.db import Task

    with setup[0].sessions() as s:
        assert task_progress(s, s.get(Task, parent.id))["phase"] == "WAITING_REMOTE"
    tasks.update(
        parent.id,
        owned.run_token,
        status="WAITING_HUMAN",
        checkpoint={"reason": "remote Pi approval required"},
    )
    manager.control(value["id"], "approve")
    claimed = manager.claim(a["id"])
    tasks.transition(parent.id, "resume")
    assert not manager.heartbeat(a["id"], value["id"], claimed["attempt_id"])["stop"]


def test_disabled_feature_cannot_admit_job(setup):
    manager = RemoteManager(setup[0], setup[1])
    with pytest.raises(Conflict):
        manager.create_node("node")


def test_concurrent_claims_have_one_owner_per_node(setup):
    from concurrent.futures import ThreadPoolExecutor

    if setup[0].engine.dialect.name != "postgresql":
        pytest.skip("real PostgreSQL admission locking")
    manager = remote(setup)
    a = node(manager)
    for index in range(8):
        job(manager, f"concurrent-{index}")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: manager.claim(a["id"]), range(8)))
    assert len([value for value in results if value]) == 1
    assert len([value for value in manager.jobs() if value["status"] == "QUEUED"]) == 7


def engine_node(manager, executors, protocol=3):
    value = manager.create_node("Engine node")
    manager.node_heartbeat(
        value["id"],
        {"executor": "pi", "available": True, "protocol_version": protocol, "executors": executors},
    )
    return value


def engine_job(manager, key, engine):
    value = manager.submit("Produce a bounded report", key, engine=engine)
    manager.control(value["id"], "approve")
    return value


def test_claim_dispatches_by_engine_and_legacy_nodes_only_run_pi(setup):
    manager = remote(setup)
    codex = engine_job(manager, "codex", "codex")
    pi = engine_job(manager, "pi", "pi")
    assert manager.get(codex["id"])["engine"] == "codex"
    legacy = node(manager)  # protocol-less node: a single legacy Pi executor
    assert manager.nodes()[0]["engines"] == {"pi": {"available": True}}
    assert manager.claim(legacy["id"])["id"] == pi["id"]
    assert manager.get(codex["id"])["wait_state"]["reason"] == "ENGINE_NOT_READY"
    mixed = engine_node(manager, {"pi": {"available": False}, "codex": {"available": True}})
    claimed = manager.claim(mixed["id"])
    assert claimed["id"] == codex["id"] and claimed["engine"] == "codex"
    assert manager.claim(mixed["id"]) is None  # one slot per node


def test_protocol3_node_without_the_requested_engine_never_claims(setup):
    manager = remote(setup)
    engine_job(manager, "pi", "pi")
    unavailable = engine_node(manager, {"pi": {"available": False, "reason": "credential_missing"}})
    assert manager.claim(unavailable["id"]) is None
    assert manager.nodes()[0]["protocol_status"] == "COMPATIBLE"
    # A protocol-2 node cannot smuggle protocol-3 executors into scheduling.
    forged = engine_node(manager, {"pi": {"available": True}}, protocol=2)
    assert manager.claim(forged["id"])["engine"] == "pi"
    no_executor = manager.create_node("No executor")
    manager.node_heartbeat(no_executor["id"], {"executor": "pi", "available": False, "protocol_version": 3})
    assert manager.claim(no_executor["id"]) is None


def test_bot_engine_whitelist_is_enforced_at_submit_and_claim(setup):
    from niucai.storage.db import Agent

    manager = remote(setup)
    a = engine_node(manager, {"pi": {"available": True}, "codex": {"available": True}})
    tasks = setup[2]
    parent = tasks.create(TaskCreate(title="parent", goal="delegate"))
    owned = tasks.claim()
    with pytest.raises(PermissionError, match="codex"):
        manager.submit(
            "child", "denied", parent_task_id=parent.id, parent_token=owned.run_token, engine="codex"
        )
    with setup[0].sessions.begin() as s:
        agent = s.get(Agent, "main")
        agent.definition = {**agent.definition, "allowed_engines": ["pi", "codex"]}
    value = manager.submit(
        "child", "ok", parent_task_id=parent.id, parent_token=owned.run_token, engine="codex"
    )
    assert value["engine"] == "codex"
    manager.control(value["id"], "approve")
    # Revoking the engine after submission stops the claim at the next boundary.
    with setup[0].sessions.begin() as s:
        agent = s.get(Agent, "main")
        agent.definition = {**agent.definition, "allowed_engines": ["pi"]}
    assert manager.claim(a["id"]) is None


def test_engine_is_validated_and_part_of_idempotency(setup):
    manager = remote(setup)
    with pytest.raises(ValueError):
        manager.submit("work", "bad", engine="gpt")
    first = manager.submit("work", "same", engine="codex")
    assert manager.submit("work", "same", engine="codex")["id"] == first["id"]
    with pytest.raises(Conflict):
        manager.submit("work", "same", engine="claude")
    with TestClient(create_app(setup[1], setup[0])) as client:
        admin = {"Authorization": "Bearer " + "x" * 40}
        response = client.post(
            "/api/remote/jobs", headers=admin, json={"prompt": "w", "idempotency_key": "k", "engine": "gpt"}
        )
        assert response.status_code == 422
        created = client.post(
            "/api/remote/jobs",
            headers=admin,
            json={"prompt": "w", "idempotency_key": "k2", "engine": "claude"},
        )
        assert created.status_code == 201 and created.json()["engine"] == "claude"


def test_runner_heartbeat_accepts_protocol3_executors_over_http(setup):
    manager = remote(setup)
    value = manager.create_node("http node")
    credential = {"Authorization": f"Bearer {value['token']}"}
    body = {
        "executor": "pi",
        "available": True,
        "protocol_version": 3,
        "executors": {"pi": {"available": True}, "codex": {"available": True, "profiles": ["read"]}},
    }
    with TestClient(create_app(setup[1], setup[0])) as client:
        response = client.post(f"/api/remote/runner/{value['id']}/heartbeat", headers=credential, json=body)
        assert response.status_code == 200 and response.json()["enabled"] is True
        body["executors"] = {"grok": {"available": True}}
        rejected = client.post(f"/api/remote/runner/{value['id']}/heartbeat", headers=credential, json=body)
        assert rejected.status_code == 422
    assert manager.nodes()[0]["engines"]["codex"]["profiles"] == ["read"]
