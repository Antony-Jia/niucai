"""Private KasmVNC relay. Never forwards the owner's browser cookie or API key."""

import asyncio
import base64
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from niucai.storage.db import Computer


def computer_proxy(db, settings, sessions, auth):
    router = APIRouter()

    def human():
        if not settings.computer_web_id or not settings.computer_web_upstream:
            raise HTTPException(503, "remote desktop is not configured")
        with db.sessions() as s:
            row = s.get(Computer, settings.computer_web_id)
            if not row or row.kind != "linux" or row.control != "HUMAN":
                raise HTTPException(403, "human control required")

    def target(path):
        upstream = urlsplit(settings.computer_web_upstream)
        if upstream.scheme not in {"http", "https"} or not upstream.hostname or upstream.username:
            raise HTTPException(503, "invalid remote desktop upstream")
        if upstream.path not in {"", "/"} or upstream.query or upstream.fragment:
            raise HTTPException(503, "upstream must be an origin without a path")
        # Prevent path traversal into other services on the private upstream.
        if ".." in path or "\\" in path or path.startswith("/"):
            raise HTTPException(400, "invalid remote desktop path")
        return settings.computer_web_upstream.rstrip("/") + "/" + path

    def private_auth():
        if not settings.computer_web_user:
            return {}
        value = f"{settings.computer_web_user}:{settings.computer_web_password.get_secret_value()}"
        return {"Authorization": "Basic " + base64.b64encode(value.encode()).decode()}

    @router.get("/computer/{path:path}", dependencies=auth)
    async def http(path: str, request: Request):
        await asyncio.to_thread(human)
        url = target(path or "vnc.html")
        client = httpx.AsyncClient(timeout=30, follow_redirects=False)
        try:
            upstream = await client.send(
                client.build_request("GET", url, params=request.query_params, headers=private_auth()),
                stream=True,
            )
        except httpx.HTTPError:
            await client.aclose()
            raise HTTPException(502, "remote desktop is unavailable") from None
        if upstream.is_redirect:
            await upstream.aclose()
            await client.aclose()
            raise HTTPException(502, "remote desktop redirects are not allowed")

        async def close():
            await upstream.aclose()
            await client.aclose()

        headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
        if media := upstream.headers.get("content-type"):
            headers["Content-Type"] = media
        return StreamingResponse(
            upstream.aiter_bytes(),
            status_code=upstream.status_code,
            headers=headers,
            background=BackgroundTask(close),
        )

    # KasmVNC versions may use an absolute /websockify URL despite a subpath UI.
    @router.websocket("/websockify")
    @router.websocket("/computer/websockify")
    async def socket(browser: WebSocket):
        value = browser.cookies.get(sessions.cookie)
        try:
            sessions.check_origin(browser)
            if not await asyncio.to_thread(sessions.valid, value):
                raise HTTPException(401, "invalid session")
            await asyncio.to_thread(human)
            url = target("websockify")
        except HTTPException:
            await browser.close(code=1008)
            return
        await browser.accept(
            subprotocol="binary" if "binary" in browser.scope.get("subprotocols", []) else None
        )
        url = ("wss" if url.startswith("https:") else "ws") + url[url.index(":") :]
        if browser.url.query:
            url += "?" + browser.url.query
        try:
            async with connect(
                url,
                additional_headers=private_auth(),
                origin=settings.computer_web_upstream,
                subprotocols=["binary"],
                max_size=16 * 1024 * 1024,
                open_timeout=10,
                compression=None,
            ) as upstream:

                async def allowed():
                    # Recheck BEFORE every input frame, not merely at handshake.
                    if not await asyncio.to_thread(sessions.valid, value):
                        raise HTTPException(401, "expired session")
                    await asyncio.to_thread(human)

                async def inputs():
                    while True:
                        message = await browser.receive()
                        if message["type"] == "websocket.disconnect":
                            return
                        await allowed()
                        payload = message.get("bytes")
                        await upstream.send(payload if payload is not None else message["text"])

                async def outputs():
                    async for message in upstream:
                        if isinstance(message, bytes):
                            await browser.send_bytes(message)
                        else:
                            await browser.send_text(message)

                async def revoked():
                    while True:
                        await allowed()
                        await asyncio.sleep(0.5)

                jobs = [asyncio.create_task(f()) for f in (inputs, outputs, revoked)]
                try:
                    done, _ = await asyncio.wait(jobs, return_when=asyncio.FIRST_COMPLETED)
                    for job in done:
                        job.result()
                finally:
                    for job in jobs:
                        job.cancel()
                    await asyncio.gather(*jobs, return_exceptions=True)
        except (HTTPException, WebSocketDisconnect, WebSocketException, OSError, TimeoutError):
            pass
        finally:
            if browser.client_state.name != "DISCONNECTED":
                await browser.close(code=1008)

    return router
