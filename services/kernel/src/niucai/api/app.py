import asyncio
import secrets
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select, text

from niucai.actions.adapters import DisabledAdapter
from niucai.actions.gateway import ActionGateway
from niucai.config import Settings
from niucai.context.compiler import ContextCompiler
from niucai.control.computers import ComputerManager
from niucai.control.progress import task_snapshot
from niucai.control.tasks import Conflict, Missing, TaskManager, require
from niucai.domain.schemas import ApprovalDecision, ComputerCreate, MemoryCreate, TaskCreate
from niucai.storage.db import (
    Action,
    Agent,
    Approval,
    Artifact,
    Computer,
    Database,
    Event,
    Memory,
    Task,
    as_dict,
    emit,
)
from niucai.worker import Worker


def create_app(settings=None, db=None, chat_gateway=None):
    settings = settings or Settings()
    db = db or Database(settings.database_url)
    tasks = TaskManager(db, settings)
    computers = ComputerManager(db)
    actions = ActionGateway(db, settings, DisabledAdapter())

    @asynccontextmanager
    async def lifespan(app):
        from niucai.observability import configure

        configure()
        if not settings.api_token.get_secret_value() or len(settings.api_token.get_secret_value()) < 32:
            raise RuntimeError("NIUCAI_API_TOKEN must be configured with at least 32 characters")
        if settings.auto_create_schema:
            db.create_schema()
        with db.sessions.begin() as s:
            if s.get(Agent, "main") is None:
                s.add(Agent(id="main", name="Main Agent", definition={"runtime": settings.runtime}))
        yield
        db.engine.dispose()

    app = FastAPI(title="niucai Kernel", version="0.1.0", lifespan=lifespan)
    app.state.db = db

    from niucai.api.sessions import SessionAuth

    sessions = SessionAuth(db, settings)

    def authenticated(request: Request, authorization: str = Header(default="")):
        expected = settings.api_token.get_secret_value()
        if expected and secrets.compare_digest(authorization, f"Bearer {expected}"):
            return
        if sessions.valid(request.cookies.get(sessions.cookie)):
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                sessions.check_origin(request)
            return
        raise HTTPException(
            401, "valid bearer token or browser session required", headers={"WWW-Authenticate": "Bearer"}
        )

    auth = [Depends(authenticated)]
    app.include_router(sessions.router(auth))
    from niucai.api.computer_proxy import computer_proxy

    app.include_router(computer_proxy(db, settings, sessions, auth))
    from niucai.api.chat import chat_router

    app.include_router(chat_router(db, settings, auth, chat_gateway))
    from niucai.api.computer_preview import preview_router

    app.include_router(preview_router(db, settings, auth))

    @app.exception_handler(Missing)
    async def not_found(_, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(Conflict)
    async def conflict(_, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.get("/healthz")
    def health():
        with db.sessions() as s:
            s.execute(text("SELECT 1"))
        return {"status": "ok"}

    @app.post("/api/tasks", dependencies=auth, status_code=201)
    def create_task(request: TaskCreate):
        return get_task(tasks.create(request).id)

    @app.get("/api/tasks", dependencies=auth)
    def list_tasks(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
        with db.sessions() as s:
            return [
                task_snapshot(s, t)
                for t in s.scalars(select(Task).order_by(Task.created_at.desc()).limit(limit).offset(offset))
            ]

    @app.get("/api/tasks/{task_id}", dependencies=auth)
    def get_task(task_id: str):
        with db.sessions() as s:
            return task_snapshot(s, require(s, Task, task_id))

    @app.post("/api/tasks/{task_id}/{operation}", dependencies=auth)
    def task_control(task_id: str, operation: Literal["pause", "resume", "retry", "cancel"]):
        return get_task(tasks.transition(task_id, operation).id)

    class TaskComputer(BaseModel):
        computer_id: str

    @app.put("/api/tasks/{task_id}/computer", dependencies=auth)
    def attach_task_computer(task_id: str, request: TaskComputer):
        return get_task(tasks.attach_computer(task_id, request.computer_id).id)

    @app.get("/api/context/{task_id}", dependencies=auth)
    def context(task_id: str):
        return ContextCompiler(db, settings).compile(task_id)

    @app.post("/api/computers", dependencies=auth, status_code=201)
    def create_computer(request: ComputerCreate):
        with db.sessions.begin() as s:
            computer = Computer(**request.model_dump())
            s.add(computer)
            s.flush()
            emit(s, "computer.registered", computer_id=computer.id, kind=computer.kind)
            return as_dict(computer)

    @app.get("/api/computers", dependencies=auth)
    def list_computers():
        with db.sessions() as s:
            return [as_dict(c) for c in s.scalars(select(Computer))]

    @app.get("/api/computers/{computer_id}", dependencies=auth)
    def get_computer(computer_id: str):
        with db.sessions() as s:
            return as_dict(require(s, Computer, computer_id))

    @app.post("/api/computers/{computer_id}/{operation}", dependencies=auth)
    def computer_control(
        computer_id: str, operation: Literal["take-control", "hand-back", "pause", "resume"]
    ):
        return as_dict(computers.control(computer_id, operation))

    @app.get("/api/agents", dependencies=auth)
    def list_agents():
        with db.sessions() as s:
            return [as_dict(a) for a in s.scalars(select(Agent))]

    @app.get("/api/approvals", dependencies=auth)
    def list_approvals(status: Literal["PENDING", "APPROVED", "DENIED"] = "PENDING"):
        with db.sessions() as s:
            return [
                {**as_dict(p), "action": as_dict(a)}
                for p, a in s.execute(
                    select(Approval, Action)
                    .join(Action, Action.id == Approval.action_id)
                    .where(Approval.status == status)
                )
            ]

    @app.post("/api/approvals/{approval_id}/{operation}", dependencies=auth)
    def decide_approval(approval_id: str, operation: Literal["approve", "deny"], request: ApprovalDecision):
        return as_dict(actions.decide(approval_id, operation == "approve", request.note))

    @app.get("/api/tasks/{task_id}/actions", dependencies=auth)
    def list_actions(task_id: str):
        with db.sessions() as s:
            require(s, Task, task_id)
            return [
                as_dict(a)
                for a in s.scalars(
                    select(Action).where(Action.task_id == task_id).order_by(Action.created_at)
                )
            ]

    class Reconcile(BaseModel):
        outcome: Literal["SUCCEEDED", "FAILED"]

    @app.post("/api/actions/{action_id}/reconcile", dependencies=auth)
    def reconcile(action_id: str, request: Reconcile):
        worker = Worker(db, settings, None, DisabledAdapter())
        return as_dict(worker.resolve_unknown(action_id, request.outcome))

    @app.post("/api/memories", dependencies=auth, status_code=201)
    def create_memory(request: MemoryCreate):
        with db.sessions.begin() as s:
            if request.task_id:
                require(s, Task, request.task_id)
            memory = Memory(**request.model_dump())
            s.add(memory)
            s.flush()
            emit(s, "memory.created", request.task_id, memory_id=memory.id, kind=memory.kind)
            return as_dict(memory)

    @app.get("/api/memories", dependencies=auth)
    def list_memories(limit: int = Query(50, ge=1, le=200)):
        with db.sessions() as s:
            return [
                as_dict(m) for m in s.scalars(select(Memory).order_by(Memory.created_at.desc()).limit(limit))
            ]

    @app.get("/api/tasks/{task_id}/artifacts", dependencies=auth)
    def list_artifacts(task_id: str):
        with db.sessions() as s:
            require(s, Task, task_id)
            return [as_dict(a) for a in s.scalars(select(Artifact).where(Artifact.task_id == task_id))]

    @app.get("/api/artifacts", dependencies=auth)
    def all_artifacts(limit: int = Query(50, ge=1, le=200)):
        with db.sessions() as s:
            return [
                as_dict(a)
                for a in s.scalars(select(Artifact).order_by(Artifact.created_at.desc()).limit(limit))
            ]

    @app.get("/api/artifacts/{artifact_id}", dependencies=auth)
    def artifact_metadata(artifact_id: str):
        with db.sessions() as s:
            return as_dict(require(s, Artifact, artifact_id))

    @app.get("/api/artifacts/{artifact_id}/content", dependencies=auth)
    def artifact_content(artifact_id: str):
        with db.sessions() as s:
            artifact = require(s, Artifact, artifact_id)
            root = settings.workspace.resolve()
            path = (root / artifact.path).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                raise HTTPException(404, "artifact content unavailable")
            return FileResponse(path, media_type=artifact.media_type, filename=path.name)

    def events_after(cursor, limit=100):
        with db.sessions() as s:
            return [
                as_dict(e)
                for e in s.scalars(select(Event).where(Event.id > cursor).order_by(Event.id).limit(limit))
            ]

    @app.get("/api/events", dependencies=auth)
    def list_events(after: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=200)):
        return events_after(after, limit)

    @app.get("/api/events/recent", dependencies=auth)
    def recent_events(limit: int = Query(100, ge=1, le=200)):
        with db.sessions() as s:
            return [as_dict(e) for e in s.scalars(select(Event).order_by(Event.id.desc()).limit(limit))]

    @app.websocket("/api/events")
    async def events(socket: WebSocket):
        await socket.accept()
        try:
            # First-frame authentication avoids leaking tokens in URL/proxy logs.
            message = await asyncio.wait_for(socket.receive_json(), timeout=5)
            token = settings.api_token.get_secret_value()
            bearer = (
                token
                and isinstance(message.get("token"), str)
                and secrets.compare_digest(token, message["token"])
            )
            session_id = socket.cookies.get(sessions.cookie)
            if not bearer:
                try:
                    sessions.check_origin(socket)
                    if not sessions.valid(session_id):
                        raise ValueError("invalid browser session")
                except (HTTPException, ValueError):
                    await socket.close(code=1008)
                    return
            cursor = int(message.get("after", 0))
            if cursor < 0:
                raise ValueError("invalid cursor")
            await socket.send_json({"type": "connected", "after": cursor})
            while True:
                if not bearer and not await asyncio.to_thread(sessions.valid, session_id):
                    await socket.close(code=1008)
                    return
                rows = await asyncio.to_thread(events_after, cursor)
                for event in rows:
                    event["created_at"] = event["created_at"].isoformat() + "Z"
                    await socket.send_json(event)
                    cursor = event["id"]
                if not rows:
                    await socket.send_json({"type": "heartbeat", "after": cursor})
                    await asyncio.sleep(settings.poll_seconds)
        except WebSocketDisconnect:
            pass
        except (TimeoutError, ValueError, TypeError, AttributeError):
            await socket.close(code=1008)

    return app


app = create_app()
