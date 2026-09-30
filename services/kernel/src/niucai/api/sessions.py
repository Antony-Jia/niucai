"""Single-owner browser sessions: master API key never enters browser storage."""

import hashlib
import secrets
from datetime import timedelta
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, SecretStr
from sqlalchemy import delete

from niucai.control.tasks import require
from niucai.storage.db import BrowserSession, Computer, now


class Login(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: SecretStr


class SessionAuth:
    cookie = "niucai_session"
    lifetime = 7 * 24 * 3600

    def __init__(self, db, settings):
        self.db, self.settings = db, settings

    @staticmethod
    def digest(value):
        return hashlib.sha256(value.encode()).hexdigest()

    def valid(self, value):
        if not value or len(value) > 256:
            return False
        with self.db.sessions() as s:
            row = s.get(BrowserSession, self.digest(value))
            return bool(
                row
                and row.expires_at > now()
                and secrets.compare_digest(
                    row.key_fingerprint, self.digest(self.settings.api_token.get_secret_value())
                )
            )

    def check_origin(self, request):
        try:
            origin = urlsplit(request.headers.get("origin", ""))
        except ValueError:
            raise HTTPException(403, "invalid origin") from None
        local = origin.hostname in {"localhost", "127.0.0.1", "::1"}
        if (
            origin.netloc != request.headers.get("host")
            or origin.scheme not in {"https", "http"}
            or (origin.scheme == "http" and (not local or self.settings.cookie_secure))
            or origin.username
            or origin.password
            or origin.path
            or origin.query
            or origin.fragment
        ):
            raise HTTPException(403, "same-origin HTTPS request required")

    def profile(self):
        url = self.settings.computer_web_url
        if not url.startswith("/computer/") or ".." in url or "?" in url or "#" in url:
            url = ""
        return {
            "base_url": "",
            "computer_url": url,
            "remember_token": False,
            "has_token": True,
            "platform": "pwa",
            "computer_id": self.settings.computer_web_id,
        }

    def router(self, auth):
        router = APIRouter()

        @router.post("/api/auth/session")
        def login(data: Login, request: Request, response: Response):
            self.check_origin(request)
            key = self.settings.api_token.get_secret_value()
            if not key or not secrets.compare_digest(data.token.get_secret_value(), key):
                raise HTTPException(401, "invalid connection token")
            value = secrets.token_urlsafe(32)
            with self.db.sessions.begin() as s:
                s.execute(delete(BrowserSession).where(BrowserSession.expires_at <= now()))
                old = request.cookies.get(self.cookie)
                if old:
                    s.execute(delete(BrowserSession).where(BrowserSession.id == self.digest(old)))
                s.add(
                    BrowserSession(
                        id=self.digest(value),
                        key_fingerprint=self.digest(key),
                        expires_at=now() + timedelta(seconds=self.lifetime),
                    )
                )
            response.set_cookie(
                self.cookie,
                value,
                max_age=self.lifetime,
                path="/",
                secure=self.settings.cookie_secure,
                httponly=True,
                samesite="strict",
            )
            response.headers["Cache-Control"] = "no-store"
            return self.profile()

        @router.get("/api/auth/session", dependencies=auth)
        def profile(response: Response):
            response.headers["Cache-Control"] = "no-store"
            return self.profile()

        @router.post("/api/auth/logout")
        def logout(request: Request, response: Response):
            # Logout also works for expired sessions. Revoke only this browser.
            self.check_origin(request)
            value = request.cookies.get(self.cookie)
            if value:
                with self.db.sessions.begin() as s:
                    s.execute(delete(BrowserSession).where(BrowserSession.id == self.digest(value)))
            response.delete_cookie(
                self.cookie, path="/", secure=self.settings.cookie_secure, httponly=True, samesite="strict"
            )
            return {"status": "signed_out"}

        @router.get("/api/auth/computer", dependencies=auth)
        def computer(computer_id: str):
            if not self.settings.computer_web_id or computer_id != self.settings.computer_web_id:
                raise HTTPException(403, "remote desktop is not configured for this computer")
            with self.db.sessions() as s:
                row = require(s, Computer, computer_id)
                if row.control != "HUMAN" or row.kind != "linux":
                    raise HTTPException(403, "human control required")
            return {"status": "allowed"}

        return router
