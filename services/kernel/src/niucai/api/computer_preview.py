"""Read-only browser frames, independent of actions and desktop ownership."""

import asyncio
import base64
from datetime import datetime, timezone
from time import monotonic

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from niucai.control.tasks import require
from niucai.storage.db import Computer


async def capture_browser(settings):
    from playwright.async_api import async_playwright

    # Stop this CDP client only; never close the shared browser or create a page.
    async with async_playwright() as playwright:
        browser = await playwright.chromium.connect_over_cdp(settings.browser_cdp_url, timeout=5000)
        pages = [page for context in browser.contexts for page in context.pages if not page.is_closed()]
        if not pages:
            raise RuntimeError("no browser page")
        return await pages[0].screenshot(type="jpeg", quality=65, timeout=5000)


def preview_router(db, settings, auth):
    router = APIRouter()
    lock = asyncio.Lock()
    cached = None
    captured = 0.0

    @router.get("/api/computers/{computer_id}/preview", dependencies=auth)
    async def preview(computer_id: str):
        nonlocal cached, captured
        with db.sessions() as session:
            computer = require(session, Computer, computer_id)
            if computer.kind != "linux" or settings.adapter != "local":
                raise HTTPException(503, "browser preview is unavailable for this computer")
        async with lock:
            if cached is None or monotonic() - captured >= 1:
                try:
                    frame = await asyncio.wait_for(capture_browser(settings), timeout=8)
                except Exception:
                    raise HTTPException(503, "remote browser preview is temporarily unavailable") from None
                cached = {
                    "image": "data:image/jpeg;base64," + base64.b64encode(frame).decode("ascii"),
                    "captured_at": datetime.now(timezone.utc).isoformat(),
                }
                captured = monotonic()
            return JSONResponse(cached, headers={"Cache-Control": "no-store"})

    return router
