from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from hashlib import sha256

import pytest
from fastapi.testclient import TestClient

from niucai.api.app import create_app
from niucai.control.remote import RemoteManager
from niucai.control.tasks import Conflict
from niucai.storage.db import RemoteJob, RemoteNode, now


def manager(setup):
    setup[1].remote_pi_enabled = True
    return RemoteManager(setup[0], setup[1])


def online(remote):
    node = remote.create_node("Docker", "local_docker")
    remote.node_heartbeat(node["id"], {"executor": "pi", "available": True})
    return node


def enqueue(remote):
    value = remote.submit("work", "resource-test")
    remote.control(value["id"], "approve")
    return value


def test_resource_api_compatibility_validation_and_private_credentials(setup):
    manager(setup)
    with TestClient(create_app(setup[1], setup[0])) as client:
        headers = {"Authorization": "Bearer " + "x" * 40}
        assert client.get("/api/remote/config", headers=headers).json() == {"enabled": True}
        response = client.post("/api/remote/nodes", headers=headers, json={"name": "old"})
        assert response.status_code == 201
        assert response.headers["cache-control"] == "no-store"
        node = response.json()
        assert node["resource_type"] == "unspecified"
        assert node["connection_status"] == "PENDING"
        path = f"/api/remote/nodes/{node['id']}"
        for body in [{"name": " "}, {"name": None}, {"enabled": None}, {"resource_type": "ssh"}]:
            assert client.patch(path, headers=headers, json=body).status_code == 422
        changed = client.patch(
            path, headers=headers, json={"name": "VPS", "resource_type": "remote_vps"}
        ).json()
        assert changed["name"] == "VPS" and changed["resource_type"] == "remote_vps"
        assert "token_hash" not in changed and "token" not in changed
        rotated = client.post(path + "/rotate-token", headers=headers)
        assert rotated.headers["cache-control"] == "no-store"
        old_auth = {"Authorization": f"Bearer {node['token']}"}
        new_auth = {"Authorization": f"Bearer {rotated.json()['token']}"}
        heartbeat = f"/api/remote/runner/{node['id']}/heartbeat"
        assert client.post(heartbeat, headers=old_auth, json={"available": True}).status_code == 401
        assert client.post(heartbeat, headers=new_auth, json={"available": True}).status_code == 200
        assert client.post(path + "/rotate-token", headers=new_auth).status_code == 401
        assert "token" not in client.get("/api/remote/nodes", headers=headers).text


def test_connection_status_is_independent_of_scheduling_and_capability(setup):
    remote = manager(setup)
    node = online(remote)
    remote.update_node(node["id"], {"enabled": False})
    view = remote.nodes()[0]
    assert view["status"] == "ONLINE" and view["connection_status"] == "ONLINE"
    assert not view["enabled"] and view["capabilities"]["available"]
    enqueue(remote)
    assert remote.claim(node["id"]) is None
    remote.update_node(node["id"], {"enabled": True})
    assert remote.claim(node["id"]) is not None
    with setup[0].sessions.begin() as s:
        s.get(RemoteNode, node["id"]).heartbeat_at = now() - timedelta(seconds=100)
    assert remote.nodes()[0]["connection_status"] == "OFFLINE"


@pytest.mark.parametrize("status", ["RUNNING", "CANCELLING", "LOST"])
def test_occupied_nodes_cannot_disable_or_rotate(setup, status):
    remote = manager(setup)
    node = online(remote)
    value = enqueue(remote)
    remote.claim(node["id"])
    with setup[0].sessions.begin() as s:
        s.get(RemoteJob, value["id"]).status = status
    for operation in [
        lambda: remote.update_node(node["id"], {"enabled": False}),
        lambda: remote.rotate_token(node["id"]),
    ]:
        with pytest.raises(Conflict):
            operation()
    remote.update_node(node["id"], {"name": "Renamed"})
    assert remote.nodes()[0]["active_job"] == {"id": value["id"], "status": status}
    assert remote.claim(node["id"]) is None


def test_rotation_fences_requests_authenticated_before_rotation(setup):
    remote = manager(setup)
    node = online(remote)
    old_hash = sha256(node["token"].encode()).hexdigest()
    fresh = remote.rotate_token(node["id"])
    assert fresh["token"] != node["token"]
    assert fresh["connection_status"] == "PENDING" and fresh["capabilities"] == {}
    with pytest.raises(PermissionError):
        remote.node_heartbeat(node["id"], {"executor": "pi", "available": True}, old_hash)
    remote.node_heartbeat(node["id"], {"executor": "pi", "available": True})
    enqueue(remote)
    with pytest.raises(PermissionError):
        remote.claim(node["id"], old_hash)
    assert remote.claim(node["id"], sha256(fresh["token"].encode()).hexdigest()) is not None


@pytest.mark.parametrize("operation", ["disable", "rotate"])
def test_management_and_claim_are_serialized_on_postgres(setup, operation):
    if setup[0].engine.dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL scheduling lock")
    remote = manager(setup)
    node = online(remote)
    enqueue(remote)
    old_hash = sha256(node["token"].encode()).hexdigest()

    def manage():
        try:
            return (
                remote.update_node(node["id"], {"enabled": False})
                if operation == "disable"
                else remote.rotate_token(node["id"])
            )
        except Conflict:
            return None

    def claim():
        try:
            return remote.claim(node["id"], old_hash)
        except PermissionError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.submit(manage), pool.submit(claim)
        managed, claimed = first.result(), second.result()
    assert (managed is None) != (claimed is None)


def test_forward_migration_preserves_identity_and_job(tmp_path, monkeypatch):
    import sqlalchemy as sa
    from alembic import command
    from alembic.config import Config

    url = f"sqlite:///{tmp_path}/migration.db"
    monkeypatch.setenv("NIUCAI_DATABASE_URL", url)
    config = Config("alembic.ini")
    command.upgrade(config, "d0d21011d004")
    engine = sa.create_engine(url)
    metadata = sa.MetaData()
    metadata.reflect(engine)
    with engine.begin() as s:
        s.execute(
            metadata.tables["remote_nodes"]
            .insert()
            .values(id="legacy", name="old", token_hash="hash", enabled=1, capabilities={}, created_at=now())
        )
        s.execute(
            metadata.tables["remote_jobs"]
            .insert()
            .values(
                id="job",
                node_id="legacy",
                idempotency_key="legacy",
                prompt="work",
                input_artifacts=[],
                allowed_nodes=[],
                status="FAILED",
                event_sequence=0,
                result={},
                created_at=now(),
                updated_at=now(),
            )
        )
    command.upgrade(config, "head")
    with engine.connect() as s:
        row = s.execute(sa.text("SELECT id, token_hash, resource_type FROM remote_nodes")).one()
        assert tuple(row) == ("legacy", "hash", "unspecified")
        assert s.scalar(sa.text("SELECT node_id FROM remote_jobs WHERE id='job'")) == "legacy"
    engine.dispose()
