"""Opt-in real-provider/Chromium acceptance in a fresh temporary database and workspace.

Run on Linux with the configured provider, built Pi runtime and Playwright Chromium.
No production database, browser profile or filesystem is used. The approvals below
authorize only writes to this script's temporary workspace.
"""

import asyncio
import json
import logging
from pathlib import Path
from tempfile import TemporaryDirectory

from playwright.async_api import async_playwright
from sqlalchemy import select

from niucai.actions.adapters import LocalAdapter
from niucai.agents.pi_adapter import PiDurableRuntime
from niucai.config import Settings
from niucai.control.computers import ComputerManager
from niucai.control.conversations import ConversationManager
from niucai.models.gateway import ModelGateway, ModelRouter
from niucai.storage.db import Action, Agent, Approval, Computer, Database, Event, Message, Task
from niucai.worker import Worker


async def verify():
    report = {"environment": "isolated local Linux Chromium + real configured model"}
    with TemporaryDirectory(prefix="niucai-unified-live-") as temporary:
        root = Path(temporary)
        settings = Settings(
            database_url=f"sqlite:///{root / 'test.db'}",
            workspace=root / "workspace",
            pi_storage=root / "pi",
            adapter="local",
            browser_cdp_url="http://127.0.0.1:19222",
        )
        if not (settings.openai_api_key.get_secret_value() or settings.openrouter_api_key.get_secret_value()):
            raise RuntimeError("Configure a real model provider before running live acceptance")
        db = Database(settings.database_url)
        db.create_schema()
        with db.sessions.begin() as session:
            session.add(Agent(id="main", name="Main", definition={}))
            session.add(Computer(id="live", name="Isolated Chromium", status="ONLINE"))
        gateway = ModelGateway(db, settings, ModelRouter(settings.model_config_path))
        adapter = LocalAdapter(settings)
        worker = Worker(db, settings, PiDurableRuntime(gateway), adapter)
        inbox = ConversationManager(db)

        def task_state(ident):
            with db.sessions() as session:
                return session.get(Task, ident)

        async def finish(ident):
            for _ in range(10):
                await asyncio.wait_for(worker.run_once(), 240)
                task = task_state(ident)
                if task.status == "COMPLETED":
                    return task
                if task.status == "PENDING":
                    continue
                if task.status == "WAITING_HUMAN":
                    with db.sessions() as session:
                        pending = session.scalars(
                            select(Approval)
                            .join(Action)
                            .where(Action.task_id == ident, Approval.status == "PENDING")
                        ).all()
                        for approval in pending:
                            action = session.get(Action, approval.action_id)
                            if action.spec["type"] != "files.write" or action.spec["path"] != "proof.txt":
                                raise RuntimeError("Live check requested an unexpected approval")
                    if pending:
                        for approval in pending:
                            worker.actions.decide(approval.id, True, "isolated acceptance workspace only")
                        report["approval"] = "approved actual isolated file write"
                        continue
                raise RuntimeError(f"Live round ended in {task.status}: {task.checkpoint.get('reason', '')}")
            raise RuntimeError("live round did not settle within ten continuations")

        async def serve_page(reader, writer):
            await reader.read(8192)
            body = (
                b"<html><head><title>Unified Live Proof</title></head><body><h1>blue proof</h1></body></html>"
            )
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nConnection: close\r\n\r\n" + body)
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(serve_page, "127.0.0.1", 19444)
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch_persistent_context(
                    str(root / "chromium"), headless=True, args=["--remote-debugging-port=19222"]
                )
                try:
                    conversation, _, first = inbox.send(
                        None,
                        "Remember the verification word blue. Reply briefly without tools.",
                        computer_id="live",
                    )
                    completed = await finish(first.id)
                    report["discussion"] = completed.status
                    _, _, execution = inbox.send(
                        conversation.id,
                        "Open http://127.0.0.1:19444 in the browser, read its title and text. "
                        "Do not write files. Reply briefly.",
                    )
                    running = asyncio.create_task(worker.run_once())
                    for _ in range(200):
                        with db.sessions() as session:
                            started = session.scalar(
                                select(Event.id).where(
                                    Event.task_id == execution.id, Event.type == "model.requested"
                                )
                            )
                        if started:
                            break
                        await asyncio.sleep(0.1)
                    else:
                        raise RuntimeError("real model request did not start")
                    ComputerManager(db).control("live", "take-control")
                    await asyncio.wait_for(running, 15)
                    assert task_state(execution.id).status == "PAUSED"
                    inbox.send(
                        conversation.id,
                        "Additional requirement: include the verification word blue in your final answer. "
                        "Still read the browser page.",
                    )
                    ComputerManager(db).control("live", "hand-back")
                    completed = await finish(execution.id)
                    assert "blue" in completed.checkpoint["result"].lower()
                    report["steering_takeover_handback"] = completed.status
                    assert (
                        completed.checkpoint["pi_conversation_id"]
                        == task_state(first.id).checkpoint["pi_conversation_id"]
                    )
                    with db.sessions() as session:
                        actions = session.scalars(select(Action).where(Action.task_id == execution.id)).all()
                        assert any(
                            a.status == "SUCCEEDED" and a.spec["type"] == "browser.snapshot" for a in actions
                        )
                        report["browser_actions"] = [
                            {"type": a.spec["type"], "status": a.status} for a in actions
                        ]
                    _, _, followup = inbox.send(
                        conversation.id,
                        "Save the page title and the verification word from our earlier conversation "
                        "to proof.txt using files.write. Do nothing else.",
                    )
                    await finish(followup.id)
                    assert "blue" in (settings.workspace / "proof.txt").read_text().lower()
                    with db.sessions() as session:
                        report["delivered_user_messages"] = len(
                            session.scalars(
                                select(Message).where(
                                    Message.role == "user", Message.delivered_at.is_not(None)
                                )
                            ).all()
                        )
                    report["followup_artifact"] = "actual proof.txt verified"
                finally:
                    await browser.close()
        finally:
            server.close()
            await server.wait_closed()
            await adapter.close()
            await gateway.close()
            db.engine.dispose()
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    logging.getLogger("niucai.worker").setLevel(logging.CRITICAL)
    asyncio.run(verify())
