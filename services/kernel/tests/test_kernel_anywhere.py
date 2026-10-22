from datetime import timedelta
from hashlib import sha256

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select

from niucai.api.app import create_app
from niucai.control.artifact_transport import ArtifactTransport
from niucai.control.bots import permitted
from niucai.control.collaboration import CollaborationManager
from niucai.control.instance import instance, migration_state, worker_heartbeat
from niucai.control.remote import RemoteManager
from niucai.control.tasks import Conflict
from niucai.domain.schemas import AgentDefinition, TaskCreate
from niucai.storage.db import Agent, ArtifactUpload, RemoteJob, RemoteNode, Task, WorkItem, now


def prepare(setup):
    db, settings, tasks, _ = setup
    settings.multi_bot_enabled = settings.remote_pi_enabled = True
    remote = RemoteManager(db, settings)
    nodes = [remote.create_node(name) for name in ("local", "second")]
    for node in nodes:
        remote.node_heartbeat(
            node["id"],
            {
                "executor": "pi",
                "available": True,
                "protocol_version": 2,
                "features": ["artifact-v2", "managed-workspace"],
            },
        )
    with db.sessions.begin() as session:
        session.add(
            Agent(
                id="developer",
                name="Developer",
                definition={
                    "tools": ["*"],
                    "instructions": "Preserve role rules",
                    "allowed_nodes": [nodes[0]["id"]],
                    "resource_policy": {"strategy": "fixed", "fixed_node_id": nodes[0]["id"]},
                },
            )
        )
    tasks.create(TaskCreate(title="build", goal="build", agent_id="developer"))
    task = tasks.claim()
    with db.sessions.begin() as session:
        CollaborationManager(db, settings).root(session, session.get(Task, task.id))
    job = remote.submit("write proof", "fixed-job", parent_task_id=task.id, parent_token=task.run_token)
    return db, settings, tasks, remote, nodes, task, job


def test_fixed_snapshot_pins_node_and_current_policy_revokes(setup):
    db, settings, tasks, remote, nodes, task, job = prepare(setup)
    assert job["allowed_nodes"] == [nodes[0]["id"]]
    assert job["config_snapshot"]["instructions"] == "Preserve role rules"
    with pytest.raises(PermissionError):
        remote.submit("bad", "wrong-node", [nodes[1]["id"]], task.id, task.run_token)
    with db.sessions.begin() as session:
        agent = session.get(Agent, "developer")
        agent.definition = {**agent.definition, "instructions": "Edited", "definition_version": 2}
        item = session.scalar(select(WorkItem).where(WorkItem.task_id == task.id))
        assert item.definition_snapshot["definition_version"] == 1
    remote.control(job["id"], "approve")
    assert remote.claim(nodes[1]["id"]) is None
    execution = remote.claim(nodes[0]["id"])
    with db.sessions.begin() as session:
        agent = session.get(Agent, "developer")
        agent.definition = {
            **agent.definition,
            "allowed_nodes": [nodes[1]["id"]],
            "resource_policy": {"strategy": "fixed", "fixed_node_id": nodes[1]["id"]},
        }
    assert remote.heartbeat(nodes[0]["id"], job["id"], execution["attempt_id"])["stop"]
    assert (
        remote.finish(nodes[0]["id"], job["id"], execution["attempt_id"], "COMPLETED", {})["status"]
        == "FAILED"
    )


def test_config_save_validates_references_and_increments_version(setup):
    db, settings, _, remote, nodes, _, _ = prepare(setup)
    headers = {"Authorization": "Bearer " + "x" * 40}
    with TestClient(create_app(settings, db)) as client:
        response = client.patch(
            "/api/agents/developer",
            headers=headers,
            json={
                "definition": {
                    "tools": ["*"],
                    "definition_version": 99,
                    "allowed_nodes": [nodes[0]["id"]],
                }
            },
        )
        assert response.status_code == 200
        assert response.json()["definition"]["definition_version"] == 2
        assert (
            client.post(
                "/api/agents",
                headers=headers,
                json={"id": "invalid", "name": "Invalid", "definition": {"allowed_nodes": ["missing"]}},
            ).status_code
            == 404
        )
        assert (
            client.post("/api/agents", headers=headers, json={"id": "safe", "name": "Safe"}).json()[
                "definition"
            ]["allowed_nodes"]
            == []
        )


def test_snapshot_prevents_tool_expansion_but_allows_revocation(setup):
    db, settings, _, _, _, task, _ = prepare(setup)
    with db.sessions.begin() as session:
        item = session.scalar(select(WorkItem).where(WorkItem.task_id == task.id))
        item.definition_snapshot = {**item.definition_snapshot, "tools": ["bot_list"]}
        with pytest.raises(PermissionError):
            permitted(session, session.get(Task, task.id), "shell.exec")
        permitted(session, session.get(Task, task.id), "bot_list")
        agent = session.get(Agent, "developer")
        agent.definition = {**agent.definition, "tools": []}
        with pytest.raises(PermissionError):
            permitted(session, session.get(Task, task.id), "bot_list")


def test_identity_survives_new_app_and_rejects_foreign_runner(setup):
    db, settings, _, remote, nodes, _, _ = prepare(setup)
    with db.sessions.begin() as session:
        ident = instance(session).instance_id
    with db.sessions.begin() as session:
        assert instance(session).instance_id == ident
    mismatch = remote.node_heartbeat(
        nodes[0]["id"],
        {"executor": "pi", "available": True, "protocol_version": 2, "kernel_instance_id": "foreign"},
    )
    assert not mismatch["enabled"] and mismatch["reason"] == "KERNEL_IDENTITY_MISMATCH"
    assert not remote.node_heartbeat(
        nodes[1]["id"], {"available": True, "executor": "pi", "protocol_version": 999}
    )["enabled"]
    worker_heartbeat(db, settings, "test-worker")
    with db.sessions.begin() as session:
        assert not migration_state(session)["backup_ready"]
        instance(session).admission_enabled = 0
    with pytest.raises(Conflict, match="draining"):
        remote.submit("blocked", "blocked")


def live_upload(setup):
    db, settings, _, remote, nodes, task, job = prepare(setup)
    remote.control(job["id"], "approve")
    job = remote.claim(nodes[0]["id"])
    assert job["artifact_transport"] is True
    transport = ArtifactTransport(db, settings)
    args = (nodes[0]["id"], job["id"], job["attempt_id"])
    return db, settings, remote, nodes, task, job, transport, args


def test_binary_upload_replay_finalize_hash_and_scope(setup):
    db, settings, remote, nodes, task, job, transport, args = live_upload(setup)
    content = bytes(range(256)) * 512  # Binary and larger than the old 16 KB limit.
    start = transport.start(
        *args, "proof.bin", len(content), sha256(content).hexdigest(), "application/octet-stream"
    )
    assert (
        transport.start(
            *args, "proof.bin", len(content), sha256(content).hexdigest(), "application/octet-stream"
        )["id"]
        == start["id"]
    )
    with pytest.raises(Conflict, match="incomplete"):
        transport.finalize(*args, start["id"])
    transport.chunk(*args, start["id"], 0, content)
    transport.chunk(*args, start["id"], 0, content)
    with pytest.raises(Conflict, match="changed"):
        transport.chunk(*args, start["id"], 0, b"forged")
    ref = transport.finalize(*args, start["id"])
    assert transport.finalize(*args, start["id"]) == ref
    with pytest.raises(PermissionError):
        transport.download(*args, ref["id"])
    result = remote.finish(*args, "COMPLETED", {"text": "binary proof", "files": []})
    assert result["result"]["artifacts"] == [ref]
    with db.sessions.begin() as session:
        current = session.get(RemoteJob, job["id"])
        current.status, current.lease_until = "RUNNING", now() + timedelta(seconds=15)
        current.input_artifacts = [ref]
    path, _ = transport.download(*args, ref["id"])
    assert path.read_bytes() == content
    with pytest.raises(Conflict, match="stale"):
        transport.download(nodes[1]["id"], job["id"], job["attempt_id"], ref["id"])


def test_upload_quota_checksum_path_and_attempt_fencing(setup):
    db, settings, _, _, _, job, transport, args = live_upload(setup)
    settings.artifact_max_file_bytes, settings.artifact_max_item_bytes = 10, 12
    digest = sha256(b"12345678").hexdigest()
    for filename in ["../escape", "x/y", "x\\y", ".hidden"]:
        with pytest.raises(ValueError, match="basename"):
            transport.start(*args, filename, 8, digest, "text/plain")
    first = transport.start(*args, "first", 8, digest, "text/plain")
    with pytest.raises(ValueError, match="quota"):
        transport.start(*args, "second", 8, digest, "text/plain")
    transport.chunk(*args, first["id"], 0, b"abcdefgh")
    with pytest.raises(Conflict, match="SHA256"):
        transport.finalize(*args, first["id"])
    with db.sessions.begin() as session:
        session.get(RemoteJob, job["id"]).attempt_id = "new-attempt"
    with pytest.raises(Conflict, match="stale"):
        transport.chunk(*args, first["id"], 0, b"12345678")


def test_expired_incomplete_upload_cleanup(setup):
    db, _, _, _, _, _, transport, args = live_upload(setup)
    start = transport.start(*args, "partial", 8, sha256(b"12345678").hexdigest(), "text/plain")
    transport.chunk(*args, start["id"], 0, b"1234")
    with db.sessions.begin() as session:
        session.get(ArtifactUpload, start["id"]).created_at = now() - timedelta(hours=2)
    assert transport.cleanup() == 1
    assert not (transport.root() / (start["id"] + ".part")).exists()


def test_output_contract_prevents_false_workflow_success(setup):
    db, settings, _, _, _, task, _ = prepare(setup)
    with db.sessions.begin() as session:
        current = session.get(Task, task.id)
        item = session.scalar(select(WorkItem).where(WorkItem.task_id == task.id))
        item.definition_snapshot = {**item.definition_snapshot, "output_contract": {"min_files": 1}}
        CollaborationManager(db, settings).finish_in(session, current, item, "COMPLETED", "claimed success")
        assert item.status == "FAILED" and current.status == "FAILED"
        assert "OUTPUT_CONTRACT_MISSING" in current.checkpoint["reason"]


def test_fixed_strategy_schema():
    with pytest.raises(ValidationError):
        AgentDefinition(resource_policy={"strategy": "fixed"})
    with pytest.raises(ValidationError):
        AgentDefinition(allowed_nodes=[], resource_policy={"strategy": "fixed", "fixed_node_id": "node"})


def test_connection_modes_and_node_token_isolation(setup):
    db, settings, _, remote, nodes, _, _ = prepare(setup)
    headers = {"Authorization": "Bearer " + "x" * 40}
    with TestClient(create_app(settings, db)) as client:
        connection = {
            "mode": "ssh_reverse",
            "runner_url": "http://127.0.0.1:18080",
            "management_url": "http://127.0.0.1:8080",
        }
        assert (
            client.post(
                "/api/remote/nodes", headers=headers, json={"name": "ssh", "connection": connection}
            ).status_code
            == 201
        )
        for mode, url in [
            ("ssh_reverse", "http://101.126.159.10:18080"),
            ("https", "http://example.com"),
            ("private_network", "http://101.126.159.10"),
            ("compose", "http://localhost"),
        ]:
            assert (
                client.post(
                    "/api/remote/nodes",
                    headers=headers,
                    json={"name": "unsafe", "connection": {**connection, "mode": mode, "runner_url": url}},
                ).status_code
                == 422
            )
        node_headers = {"Authorization": "Bearer " + nodes[0]["token"]}
        assert client.get("/api/kernel/identity", headers=node_headers).status_code == 401


def test_emergency_node_revoke_preserves_occupancy_until_confirmed_exit(setup):
    _, _, remote, nodes, _, job, _, args = live_upload(setup)
    remote.revoke_node(nodes[0]["id"])
    assert remote.get(job["id"])["status"] == "CANCELLING"
    assert remote.nodes()[0]["active_job"]["id"] == job["id"]
    assert remote.heartbeat(*args)["stop"]
    assert remote.claim(nodes[0]["id"]) is None
    assert remote.finish(*args, "COMPLETED", {})["status"] == "CANCELLED"
    assert remote.nodes()[0]["active_job"] is None


def test_fixed_offline_timeout_is_recoverable_and_does_not_fail_over(setup):
    db, _, _, remote, nodes, _, job = prepare(setup)
    remote.control(job["id"], "approve")
    with db.sessions.begin() as session:
        session.get(RemoteNode, nodes[0]["id"]).heartbeat_at = None
    assert remote.get(job["id"])["wait_state"]["reason"] == "NODE_OFFLINE"
    assert remote.claim(nodes[1]["id"]) is None
    with db.sessions.begin() as session:
        stored = session.get(RemoteJob, job["id"])
        stored.wait_state = {"reason": "NODE_OFFLINE", "since": (now() - timedelta(hours=1)).isoformat()}
    expired = remote.get(job["id"])
    assert expired["status"] == "FAILED" and expired["result"]["recoverable"]
    retry = remote.control(job["id"], "retry")
    assert retry["status"] == "WAITING_APPROVAL" and retry["allowed_nodes"] == [nodes[0]["id"]]


def test_drain_allows_existing_wait_and_reply_to_finish(setup):
    db, settings, tasks, _, _, _, _ = prepare(setup)
    # Close the fixture's parent, then create a fresh coordinated workflow.
    with db.sessions.begin() as session:
        for task in session.scalars(select(Task)):
            task.status = "CANCELLED"
    tasks.create(TaskCreate(title="main", goal="coordinate"))
    parent = tasks.claim()
    collaboration = CollaborationManager(db, settings)
    handoff = collaboration.send(parent.id, parent.run_token, "new-work", "developer", "work")
    child = tasks.claim()
    with db.sessions.begin() as session:
        instance(session).admission_enabled = 0
    assert collaboration.wait(parent.id, parent.run_token, "wait", [handoff["id"]])["status"] == "WAITING_BOT"
    assert (
        collaboration.reply(child.id, child.run_token, handoff["id"], "completed")["complete"] == "completed"
    )
