from datetime import timedelta

from fastapi.testclient import TestClient

from niucai.api.app import create_app
from niucai.control.pairing import code_hash
from niucai.storage.db import PairingCode, now


def client(setup):
    db, settings, *_ = setup
    settings.remote_pi_enabled = True
    auth = {"Authorization": f"Bearer {settings.api_token.get_secret_value()}"}
    return TestClient(create_app(settings, db)), auth, db


def test_pairing_redeem_heartbeat_and_topology(setup):
    api, auth, _ = client(setup)
    created = api.post("/api/pairing", json={"name": "VPS-1", "resource_type": "remote_vps"}, headers=auth)
    assert created.status_code == 201 and created.headers["cache-control"] == "no-store"
    code = created.json()["code"]
    assert len(code) == 9 and code[4] == "-"

    assert api.post("/api/pairing", json={"name": "x"}).status_code == 401
    redeemed = api.post("/api/pairing/redeem", json={"code": code.lower(), "runner_version": "1.0"})
    assert redeemed.status_code == 201
    pair = redeemed.json()
    assert pair["token"] and pair["kernel_instance_id"]
    # 一次性：重复兑换失败
    assert api.post("/api/pairing/redeem", json={"code": code}).status_code == 409

    node_auth = {"Authorization": f"Bearer {pair['token']}"}
    beat = api.post(
        f"/api/remote/runner/{pair['node_id']}/heartbeat",
        json={
            "available": True,
            "protocol_version": 3,
            "executors": {"pi": {"available": True}},
            "platform": "linux",
            "runner_version": "1.1",
            "preview_url": "https://vps-1.example.com/preview",
        },
        headers=node_auth,
    )
    assert beat.status_code == 200 and beat.json()["enabled"] is True

    topology = api.get("/api/topology", headers=auth).json()
    assert topology["kernel"]["remote_enabled"] is True
    [entry] = topology["nodes"]
    assert entry["name"] == "VPS-1" and entry["status"] == "ONLINE"
    assert entry["platform"] == "linux" and entry["runner_version"] == "1.1"
    assert entry["preview_url"] == "https://vps-1.example.com/preview"
    assert entry["slots"] == 1 and entry["busy"] == 0 and entry["bots"] == []
    assert entry["paired_at"]

    updated = api.patch(f"/api/remote/nodes/{pair['node_id']}", json={"slots": 3}, headers=auth)
    assert updated.status_code == 200 and updated.json()["capacity"] == 3
    invalid = api.patch(f"/api/remote/nodes/{pair['node_id']}", json={"slots": 0}, headers=auth)
    assert invalid.status_code == 422


def test_heartbeat_publishes_selectable_models_per_engine(setup):
    api, auth, _ = client(setup)
    code = api.post("/api/pairing", json={"name": "models"}, headers=auth).json()["code"]
    pair = api.post("/api/pairing/redeem", json={"code": code}).json()
    url = f"/api/remote/runner/{pair['node_id']}/heartbeat"
    node_auth = {"Authorization": f"Bearer {pair['token']}"}
    beat = {
        "available": True,
        "protocol_version": 3,
        "executors": {
            "pi": {"available": True, "model": "a", "models": ["a", "b"], "efforts": ["low", "high"]},
            "claude": {"available": True},
        },
    }
    assert api.post(url, json=beat, headers=node_auth).status_code == 200
    [entry] = api.get("/api/topology", headers=auth).json()["nodes"]
    assert entry["engines"]["pi"]["models"] == ["a", "b"]
    assert entry["engines"]["claude"]["models"] == []  # 旧 Runner 不上报时是空列表
    assert entry["engines"]["pi"]["efforts"] == ["low", "high"]
    assert entry["engines"]["claude"]["efforts"] == []

    beat["executors"]["pi"]["models"] = ["x" * 201]
    assert api.post(url, json=beat, headers=node_auth).status_code == 422


def test_expired_and_unknown_codes_are_rejected(setup):
    api, auth, db = client(setup)
    code = api.post("/api/pairing", json={"name": "old"}, headers=auth).json()["code"]
    with db.sessions.begin() as s:
        s.query(PairingCode).filter_by(code_hash=code_hash(code)).one().expires_at = now() - timedelta(
            seconds=1
        )
    assert api.post("/api/pairing/redeem", json={"code": code}).status_code == 409
    assert api.post("/api/pairing/redeem", json={"code": "AAAA-BBBB"}).status_code == 409


def test_preview_url_must_be_https_without_credentials(setup):
    api, auth, _ = client(setup)
    code = api.post("/api/pairing", json={"name": "n"}, headers=auth).json()["code"]
    pair = api.post("/api/pairing/redeem", json={"code": code}).json()
    node_auth = {"Authorization": f"Bearer {pair['token']}"}
    url = f"/api/remote/runner/{pair['node_id']}/heartbeat"
    for bad in ("http://vps.example.com/", "https://u:p@vps.example.com/", "https://vps.example.com/?t=1"):
        rejected = api.post(url, json={"available": True, "preview_url": bad}, headers=node_auth)
        assert rejected.status_code == 422
    ok = api.post(url, json={"available": True, "preview_url": "http://127.0.0.1:6901/"}, headers=node_auth)
    assert ok.status_code == 200


def test_pairing_requires_remote_enabled(setup):
    api, auth, _ = client(setup)
    setup[1].remote_pi_enabled = False
    assert api.post("/api/pairing", json={"name": "n"}, headers=auth).status_code == 409
