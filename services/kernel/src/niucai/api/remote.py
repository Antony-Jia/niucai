import json
import secrets
from hashlib import sha256
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator

from niucai.control.remote import RemoteManager
from niucai.storage.db import RemoteNode


class JobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=1, max_length=16000)
    idempotency_key: str = Field(min_length=1, max_length=200)
    allowed_nodes: list[str] = Field(default_factory=list, max_length=32)
    parent_task_id: str | None = None


class Attempt(BaseModel):
    attempt_id: str = Field(min_length=1, max_length=36)


class Progress(Attempt):
    sequence: int = Field(ge=1)
    kind: str = Field(min_length=1, max_length=60)
    data: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def bounded(self):
        if len(json.dumps(self.data).encode()) > 16000:
            raise ValueError("event payload exceeds 16 KB")
        return self


class Finished(Attempt):
    status: Literal["COMPLETED", "FAILED", "CANCELLED"]
    result: dict

    @model_validator(mode="after")
    def bounded(self):
        if len(json.dumps(self.result).encode()) > 300000:
            raise ValueError("result exceeds 300 KB")
        return self


def remote_router(db, settings, auth):
    router = APIRouter()
    manager = RemoteManager(db, settings)

    class NodeCreate(BaseModel):
        name: str = Field(min_length=1, max_length=200)

    class Capabilities(BaseModel):
        executor: Literal["pi"] = "pi"
        available: bool
        version: str = Field(default="", max_length=100)

    def node_auth(node_id: str, authorization: str = Header(default="")):
        with db.sessions() as s:
            node = s.get(RemoteNode, node_id)
            supplied = authorization.removeprefix("Bearer ")
            if (
                not authorization.startswith("Bearer ")
                or node is None
                or not secrets.compare_digest(node.token_hash, sha256(supplied.encode()).hexdigest())
            ):
                raise HTTPException(401, "valid node credential required")
        return node_id

    @router.post("/api/remote/nodes", dependencies=auth, status_code=201)
    def create_node(request: NodeCreate):
        return manager.create_node(request.name)

    @router.get("/api/remote/nodes", dependencies=auth)
    def nodes():
        return manager.nodes()

    @router.post("/api/remote/jobs", dependencies=auth, status_code=201)
    def submit(request: JobCreate):
        return manager.submit(
            request.prompt, request.idempotency_key, request.allowed_nodes, request.parent_task_id
        )

    @router.get("/api/remote/jobs", dependencies=auth)
    def jobs(parent_task_id: str | None = None):
        return manager.jobs(parent_task_id)

    @router.get("/api/remote/jobs/{job_id}", dependencies=auth)
    def job(job_id: str):
        return manager.get(job_id)

    @router.get("/api/remote/jobs/{job_id}/events", dependencies=auth)
    def events(job_id: str, after: int = Query(0, ge=0)):
        return manager.events(job_id, after)

    @router.post("/api/remote/jobs/{job_id}/{operation}", dependencies=auth)
    def control(job_id: str, operation: Literal["approve", "deny", "cancel", "retry"]):
        return manager.control(job_id, operation)

    @router.post("/api/remote/runner/{node_id}/heartbeat")
    def node_heartbeat(request: Capabilities, node_id: str = Depends(node_auth)):
        return manager.node_heartbeat(node_id, request.model_dump())

    @router.post("/api/remote/runner/{node_id}/claim")
    def claim(node_id: str = Depends(node_auth)):
        return manager.claim(node_id)

    @router.post("/api/remote/runner/{node_id}/jobs/{job_id}/heartbeat")
    def heartbeat(job_id: str, request: Attempt, node_id: str = Depends(node_auth)):
        return manager.heartbeat(node_id, job_id, request.attempt_id)

    @router.post("/api/remote/runner/{node_id}/jobs/{job_id}/events")
    def progress(job_id: str, request: Progress, node_id: str = Depends(node_auth)):
        return manager.event(
            node_id, job_id, request.attempt_id, request.sequence, request.kind, request.data
        )

    @router.post("/api/remote/runner/{node_id}/jobs/{job_id}/finish")
    def finish(job_id: str, request: Finished, node_id: str = Depends(node_auth)):
        return manager.finish(node_id, job_id, request.attempt_id, request.status, request.result)

    return router
