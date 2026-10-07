from fastapi.testclient import TestClient
from sqlalchemy import select

from niucai.api.app import create_app
from niucai.storage.db import Action, Computer


def test_preview_auth_read_only_and_cache(setup, monkeypatch):
    db, settings, _, ident = setup
    settings.adapter = "local"
    calls = []

    async def capture(_):
        calls.append(True)
        return b"test-jpeg"

    monkeypatch.setattr("niucai.api.computer_preview.capture_browser", capture)
    auth = {"Authorization": f"Bearer {settings.api_token.get_secret_value()}"}
    path = f"/api/computers/{ident}/preview"
    with TestClient(create_app(settings, db)) as client:
        assert client.get(path).status_code == 401
        assert client.get("/api/computers/missing/preview", headers=auth).status_code == 404
        response = client.get(path, headers=auth)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["image"] == "data:image/jpeg;base64,dGVzdC1qcGVn"
        assert client.get(path, headers=auth).json() == response.json()
        assert calls == [True]
    with db.sessions() as session:
        assert session.get(Computer, ident).control == "AGENT"
        assert list(session.scalars(select(Action))) == []


def test_preview_failure_does_not_leak_connection_details(setup, monkeypatch):
    db, settings, _, ident = setup
    settings.adapter = "local"

    async def capture(_):
        raise RuntimeError("private-secret-cdp-url")

    monkeypatch.setattr("niucai.api.computer_preview.capture_browser", capture)
    auth = {"Authorization": f"Bearer {settings.api_token.get_secret_value()}"}
    with TestClient(create_app(settings, db)) as client:
        response = client.get(f"/api/computers/{ident}/preview", headers=auth)
        assert response.status_code == 503
        assert "private-secret" not in response.text
        settings.adapter = "disabled"
        assert client.get(f"/api/computers/{ident}/preview", headers=auth).status_code == 503
