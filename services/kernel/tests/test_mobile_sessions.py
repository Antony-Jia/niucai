from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from starlette.websockets import WebSocketDisconnect

from niucai.api.app import create_app
from niucai.api.sessions import SessionAuth
from niucai.storage.db import BrowserSession, Computer, now

ORIGIN = {"Origin": "https://testserver"}


def login(client, settings):
    return client.post(
        "/api/auth/session",
        headers=ORIGIN,
        json={"token": settings.api_token.get_secret_value()},
    )


def test_cookie_persistence_csrf_and_logout(setup):
    db, settings, _, _ = setup
    app = create_app(settings, db)
    with TestClient(app, base_url="https://testserver") as c:
        assert c.get("/api/auth/session").status_code == 401
        assert c.post("/api/auth/session", json={"token": "x" * 40}).status_code == 403
        assert c.post("/api/auth/session", headers=ORIGIN, json={"token": "bad"}).status_code == 401
        result = login(c, settings)
        assert result.status_code == 200
        assert "x" * 40 not in result.text
        cookie = result.headers["set-cookie"]
        assert all(v in cookie for v in ("HttpOnly", "Secure", "SameSite=strict", "Path=/"))
        value = c.cookies.get(SessionAuth.cookie)
        with db.sessions() as s:
            row = s.scalar(select(BrowserSession))
            assert row.id == SessionAuth.digest(value) and row.id != value
        assert c.get("/api/tasks").status_code == 200
        data = {"title": "手机任务", "goal": "persist"}
        assert c.post("/api/tasks", json=data).status_code == 403
        assert c.post("/api/tasks", json=data, headers={"Origin": "https://evil.test"}).status_code == 403
        task = c.post("/api/tasks", json=data, headers=ORIGIN).json()
    with TestClient(create_app(settings, db), base_url="https://testserver") as c:
        c.cookies.set(SessionAuth.cookie, value)
        assert c.get(f"/api/tasks/{task['id']}").json()["title"] == "手机任务"
        assert c.post("/api/auth/logout", headers=ORIGIN).status_code == 200
        assert not SessionAuth(db, settings).valid(value)
        assert c.get("/api/tasks").status_code == 401


def test_expiry_rotation_and_bearer_compatibility(setup):
    db, settings, _, _ = setup
    with TestClient(create_app(settings, db), base_url="https://testserver") as c:
        login(c, settings)
        first = c.cookies.get(SessionAuth.cookie)
        login(c, settings)
        assert not SessionAuth(db, settings).valid(first)
        with db.sessions.begin() as s:
            row = s.get(BrowserSession, SessionAuth.digest(c.cookies.get(SessionAuth.cookie)))
            row.expires_at = now() - timedelta(seconds=1)
        assert c.get("/api/tasks").status_code == 401
        login(c, settings)
        settings.api_token = "y" * 40
        assert c.get("/api/tasks").status_code == 401
        assert c.get("/api/tasks", headers={"Authorization": "Bearer " + "y" * 40}).status_code == 200


def test_websocket_cookie_requires_origin_and_revokes(setup):
    db, settings, _, _ = setup
    with TestClient(create_app(settings, db), base_url="https://testserver") as c:
        login(c, settings)
        with c.websocket_connect(
            "wss://testserver/api/events", headers={"Origin": "https://evil.test"}
        ) as ws:
            ws.send_json({"after": 0})
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()
        with c.websocket_connect("wss://testserver/api/events", headers=ORIGIN) as ws:
            ws.send_json({"after": 0})
            assert ws.receive_json()["type"] == "connected"
            c.post("/api/auth/logout", headers=ORIGIN)
            with pytest.raises(WebSocketDisconnect):
                for _ in range(30):
                    ws.receive_json()


def test_desktop_entry_is_bound_to_configured_human_computer(setup):
    db, settings, _, ident = setup
    settings.computer_web_id = ident
    settings.computer_web_upstream = "http://computer:6901"
    with TestClient(create_app(settings, db), base_url="https://testserver") as c:
        assert c.get("/computer/vnc.html").status_code == 401
        login(c, settings)
        assert c.get("/computer/vnc.html").status_code == 403
        assert c.get("/api/auth/computer", params={"computer_id": ident}).status_code == 403
        c.post(f"/api/computers/{ident}/take-control", headers=ORIGIN)
        assert c.get("/api/auth/computer", params={"computer_id": ident}).status_code == 200
        assert c.get("/api/auth/computer", params={"computer_id": "wrong"}).status_code == 403
        c.post(f"/api/computers/{ident}/hand-back", headers=ORIGIN)
        with db.sessions() as s:
            assert s.get(Computer, ident).control == "AGENT"
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect("wss://testserver/computer/websockify", headers=ORIGIN):
                pass


def test_local_development_origin_is_explicit(setup):
    db, settings, _, _ = setup
    with TestClient(create_app(settings, db), base_url="http://localhost") as c:
        data = {"token": "x" * 40}
        assert (
            c.post("/api/auth/session", headers={"Origin": "http://localhost"}, json=data).status_code == 403
        )
        settings.cookie_secure = False
        assert (
            c.post("/api/auth/session", headers={"Origin": "http://localhost"}, json=data).status_code == 200
        )
        assert c.get("/api/tasks").status_code == 200
        assert c.post("/api/auth/logout", headers={"Origin": "http://evil.test"}).status_code == 403


@pytest.mark.parametrize("revoke", ["hand-back", "logout"])
def test_live_desktop_connection_cannot_input_after_revocation(setup, monkeypatch, revoke):
    import asyncio
    import importlib

    proxy = importlib.import_module("niucai.api.computer_proxy")
    db, settings, _, ident = setup
    settings.computer_web_id = ident
    settings.computer_web_upstream = "http://computer:6901"
    received = []

    class FakeUpstream:
        async def __aenter__(self):
            self.output = asyncio.Queue()
            return self

        async def __aexit__(self, *_):
            pass

        async def send(self, payload):
            received.append(payload)
            await self.output.put(payload)

        def __aiter__(self):
            return self

        async def __anext__(self):
            return await self.output.get()

    monkeypatch.setattr(proxy, "connect", lambda *a, **kw: FakeUpstream())
    with TestClient(create_app(settings, db), base_url="https://testserver") as c:
        login(c, settings)
        c.post(f"/api/computers/{ident}/take-control", headers=ORIGIN)
        with c.websocket_connect("wss://testserver/computer/websockify", headers=ORIGIN) as ws:
            ws.send_bytes(b"first input")
            assert ws.receive_bytes() == b"first input"
            path = f"/api/computers/{ident}/hand-back" if revoke == "hand-back" else "/api/auth/logout"
            c.post(path, headers=ORIGIN)
            ws.send_bytes(b"revoked input")
            with pytest.raises(WebSocketDisconnect):
                ws.receive_bytes()
    assert received == [b"first input"]


def test_desktop_http_does_not_leak_browser_credentials(setup, monkeypatch):
    import importlib

    import httpx

    proxy = importlib.import_module("niucai.api.computer_proxy")
    db, settings, _, ident = setup
    settings.computer_web_id = ident
    settings.computer_web_upstream = "http://computer:6901"
    settings.computer_web_user = "private-user"
    settings.computer_web_password = "private-password"
    requests = []

    def upstream(request):
        requests.append(request)
        return httpx.Response(200, text="desktop", headers={"set-cookie": "private-cookie=secret"})

    original = httpx.AsyncClient
    monkeypatch.setattr(
        proxy.httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(upstream), **kw)
    )
    with TestClient(create_app(settings, db), base_url="https://testserver") as c:
        login(c, settings)
        c.post(f"/api/computers/{ident}/take-control", headers=ORIGIN)
        response = c.get("/computer/vnc.html")
        assert response.text == "desktop" and response.headers["Cache-Control"] == "no-store"
        assert "set-cookie" not in response.headers
        assert "cookie" not in requests[0].headers
        assert requests[0].headers["authorization"].startswith("Basic ")
        assert requests[0].url == "http://computer:6901/vnc.html"
