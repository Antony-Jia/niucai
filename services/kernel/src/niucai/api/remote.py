import json
import secrets
from hashlib import sha256
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from niucai.control.remote import RemoteManager
from niucai.storage.db import RemoteNode


def validate_preview_url(value):
    """节点画面预览地址：Desktop 直连，必须 HTTPS（回环地址允许 HTTP），不得内嵌凭据。"""
    from urllib.parse import urlsplit

    if not value:
        return value
    url = urlsplit(value)
    loopback = url.hostname in {"localhost", "127.0.0.1", "::1"}
    if url.scheme not in ({"https", "http"} if loopback else {"https"}) or not url.hostname:
        raise ValueError("preview_url must be HTTPS (HTTP only for loopback)")
    if url.username or url.password or url.query or url.fragment:
        raise ValueError("preview_url cannot contain credentials, query or fragment")
    return value


def validate_connection(value):
    from urllib.parse import urlsplit

    if not value:
        return  # Legacy nodes remain explicitly unverified.
    if set(value) - {"mode", "runner_url", "management_url", "kernel_instance_id"}:
        raise ValueError("unknown connection field")
    mode = value.get("mode")
    if mode not in {"compose", "private_network", "https", "ssh_reverse"}:
        raise ValueError("invalid connection mode")
    for key in ("runner_url", "management_url"):
        url = urlsplit(value.get(key, ""))
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
            raise ValueError("connection requires credential-free HTTP/HTTPS URLs")
        if url.query or url.fragment:
            raise ValueError("connection URLs cannot contain query or fragment")
        if key == "runner_url":
            if mode == "ssh_reverse" and (url.scheme != "http" or url.hostname != "127.0.0.1"):
                raise ValueError("SSH reverse mode requires HTTP on IPv4 loopback")
            if mode == "https" and url.scheme != "https":
                raise ValueError("public connection requires HTTPS")
            if mode == "private_network":
                import ipaddress

                try:
                    address = ipaddress.ip_address(url.hostname)
                except ValueError as exc:
                    raise ValueError("private network HTTP requires an explicit private IP") from exc
                if not address.is_private or address.is_unspecified:
                    raise ValueError("private network requires a private address")
            if mode == "compose" and url.hostname != "api":
                raise ValueError("managed Compose mode requires api service address")


class JobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=1, max_length=16000)
    idempotency_key: str = Field(min_length=1, max_length=200)
    allowed_nodes: list[str] = Field(default_factory=list, max_length=32)
    parent_task_id: str | None = None
    artifacts: list[dict] = Field(default_factory=list, max_length=16)
    engine: Literal["pi", "codex", "claude", "hermes"] | None = None
    permission_profile: Literal["read", "workspace", "full"] | None = None
    resume_job_id: str | None = Field(default=None, min_length=1, max_length=36)


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


class BoardCall(BaseModel):
    model_config = ConfigDict(extra="forbid")
    args: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def bounded(self):
        if len(json.dumps(self.args).encode()) > 32000:
            raise ValueError("board tool arguments exceed 32 KB")
        return self


class UploadStart(Attempt):
    filename: str = Field(min_length=1, max_length=200)
    size: int = Field(ge=0)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    media_type: str = "application/octet-stream"


def remote_router(db, settings, auth):
    router = APIRouter()
    manager = RemoteManager(db, settings)

    class NodeCreate(BaseModel):
        model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
        name: str = Field(min_length=1, max_length=200)
        resource_type: Literal["local_docker", "remote_vps", "windows_local", "unspecified"] = "unspecified"
        connection: dict = Field(default_factory=dict)

        @model_validator(mode="after")
        def valid_connection(self):
            validate_connection(self.connection)
            return self

    class NodeUpdate(BaseModel):
        model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
        name: str | None = Field(default=None, min_length=1, max_length=200)
        resource_type: Literal["local_docker", "remote_vps", "windows_local", "unspecified"] | None = None
        enabled: bool | None = None
        connection: dict | None = None
        slots: int | None = Field(default=None, ge=1, le=16)

        @model_validator(mode="after")
        def not_null(self):
            if any(getattr(self, key) is None for key in self.model_fields_set):
                raise ValueError("updated fields cannot be null")
            if self.connection is not None:
                validate_connection(self.connection)
            return self

    class ExecutorStatus(BaseModel):
        available: bool
        version: str = Field(default="", max_length=100)
        model: str = Field(default="", max_length=200)
        # 节点上这个引擎可选的模型；Desktop 的 Bot 表单据此给下拉选项。
        models: list[Annotated[str, Field(max_length=200)]] = Field(default_factory=list, max_length=50)
        # 这个引擎接受的推理强度（Pi thinking / Codex reasoning effort / Claude effort）。
        efforts: list[str] = Field(default_factory=list, max_length=10)
        profiles: list[str] = Field(default_factory=list, max_length=8)
        reason: str = Field(default="", max_length=100)
        # remote：引擎（Hermes）在别处运行，隔离由其部署决定。
        sandbox: Literal["engine", "container", "remote", ""] = ""

    class Capabilities(BaseModel):
        # Windows Runner 可以只安装 Codex / Claude；none 表示未启用任何引擎。
        executor: Literal["pi", "codex", "claude", "hermes", "none"] = "pi"
        available: bool
        executors: dict[Literal["pi", "codex", "claude", "hermes"], ExecutorStatus] | None = None
        version: str = Field(default="", max_length=100)
        protocol_version: int | None = Field(default=None, ge=1)
        kernel_instance_id: str | None = Field(default=None, max_length=36)
        features: list[str] = Field(default_factory=list, max_length=32)
        node_model: dict[str, str] = Field(default_factory=dict)
        platform: Literal["linux", "windows", "macos"] | None = None
        runner_version: str | None = Field(default=None, max_length=50)
        workdir_allow: list[str] = Field(default_factory=list, max_length=20)
        preview_url: str | None = Field(default=None, max_length=500)

        @model_validator(mode="after")
        def valid_preview(self):
            validate_preview_url(self.preview_url)
            return self

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
    def create_node(request: NodeCreate, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return manager.create_node(request.name, request.resource_type, request.connection)

    @router.get("/api/remote/config", dependencies=auth)
    def config():
        return {"enabled": settings.remote_pi_enabled}

    @router.patch("/api/remote/nodes/{node_id}", dependencies=auth)
    def update_node(node_id: str, request: NodeUpdate):
        return manager.update_node(node_id, request.model_dump(exclude_unset=True))

    @router.post("/api/remote/nodes/{node_id}/revoke", dependencies=auth)
    def revoke_node(node_id: str):
        return manager.revoke_node(node_id)

    @router.post("/api/remote/nodes/{node_id}/rotate-token", dependencies=auth)
    def rotate_token(node_id: str, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return manager.rotate_token(node_id)

    @router.get("/api/remote/nodes", dependencies=auth)
    def nodes():
        return manager.nodes()

    @router.post("/api/remote/jobs", dependencies=auth, status_code=201)
    def submit(request: JobCreate):
        return manager.submit(
            request.prompt,
            request.idempotency_key,
            request.allowed_nodes,
            request.parent_task_id,
            artifacts=request.artifacts,
            engine=request.engine,
            permission_profile=request.permission_profile,
            resume_job_id=request.resume_job_id,
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
    def node_heartbeat(
        request: Capabilities, node_id: str = Depends(node_auth), authorization: str = Header()
    ):
        return manager.node_heartbeat(
            node_id,
            request.model_dump(exclude_none=True),
            sha256(authorization.removeprefix("Bearer ").encode()).hexdigest(),
        )

    @router.post("/api/remote/runner/{node_id}/claim")
    def claim(
        node_id: str = Depends(node_auth),
        authorization: str = Header(),
        wait: int = Query(0, ge=0, le=25),
    ):
        import time

        # 长轮询：最多挂起 wait 秒，每秒重试一次，有可领取的卡片/作业立即返回。
        digest = sha256(authorization.removeprefix("Bearer ").encode()).hexdigest()
        deadline = time.monotonic() + wait
        while True:
            claimed = manager.claim(node_id, digest)
            if claimed or time.monotonic() >= deadline:
                return claimed
            time.sleep(1)

    @router.post("/api/remote/runner/{node_id}/jobs/{job_id}/board/{tool}")
    def board_tool(
        job_id: str,
        tool: str,
        request: BoardCall,
        node_id: str = Depends(node_auth),
        board_token: str = Header(alias="X-Board-Token", min_length=1, max_length=200),
    ):
        from niucai.control.cards import CardManager

        return CardManager(db, settings).board_call(node_id, job_id, board_token, tool, request.args)

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

    @router.get("/api/remote/runner/{node_id}/jobs/{job_id}/artifacts/{artifact_id}")
    def input_artifact(job_id: str, artifact_id: str, attempt_id: str, node_id: str = Depends(node_auth)):
        from fastapi.responses import FileResponse

        from niucai.control.artifact_transport import ArtifactTransport

        path, media_type = ArtifactTransport(db, settings).download(node_id, job_id, attempt_id, artifact_id)
        return FileResponse(path, media_type=media_type, headers={"Cache-Control": "no-store"})

    @router.post("/api/remote/runner/{node_id}/jobs/{job_id}/uploads", status_code=201)
    def upload_start(job_id: str, request: UploadStart, node_id: str = Depends(node_auth)):
        from niucai.control.artifact_transport import ArtifactTransport

        return ArtifactTransport(db, settings).start(node_id, job_id, **request.model_dump())

    @router.put("/api/remote/runner/{node_id}/jobs/{job_id}/uploads/{upload_id}")
    async def upload_chunk(
        job_id: str,
        upload_id: str,
        request: Request,
        attempt_id: str,
        offset: int = Query(ge=0),
        node_id: str = Depends(node_auth),
    ):
        import asyncio

        from niucai.control.artifact_transport import CHUNK_BYTES, ArtifactTransport

        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > CHUNK_BYTES:
                raise HTTPException(413, "chunk exceeds 1 MiB")
            body.extend(chunk)
        return await asyncio.to_thread(
            ArtifactTransport(db, settings).chunk, node_id, job_id, attempt_id, upload_id, offset, bytes(body)
        )

    @router.post("/api/remote/runner/{node_id}/jobs/{job_id}/uploads/{upload_id}/finalize")
    def upload_finalize(job_id: str, upload_id: str, request: Attempt, node_id: str = Depends(node_auth)):
        from niucai.control.artifact_transport import ArtifactTransport

        return ArtifactTransport(db, settings).finalize(node_id, job_id, request.attempt_id, upload_id)

    return router
