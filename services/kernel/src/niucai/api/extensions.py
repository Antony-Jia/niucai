"""技能库与 MCP 服务器登记的 REST 接口（单用户 Bearer Token）。"""

from fastapi import APIRouter
from sqlalchemy import select

from niucai.control import mcp
from niucai.control.tasks import Conflict, require
from niucai.domain.schemas import McpHttp, McpServerCreate, McpServerPatch, McpStdio, SkillCreate, SkillPatch
from niucai.storage.db import McpServer, Skill, as_dict, emit


def unique(session, model, name, ident=None):
    other = session.scalar(select(model).where(model.name == name))
    if other and other.id != ident:
        raise Conflict(f"name {name} already exists")


def extension_router(db, settings, auth):
    router = APIRouter(dependencies=auth)

    def skill_dict(row):
        return {**as_dict(row), "enabled": bool(row.enabled)}

    @router.get("/api/skills")
    def list_skills():
        with db.sessions() as session:
            return [skill_dict(r) for r in session.scalars(select(Skill).order_by(Skill.name))]

    @router.post("/api/skills", status_code=201)
    def create_skill(request: SkillCreate):
        with db.sessions.begin() as session:
            unique(session, Skill, request.name)
            row = Skill(**{**request.model_dump(), "enabled": int(request.enabled)})
            session.add(row)
            session.flush()
            emit(session, "skill.created", None, skill_id=row.id, name=row.name)
            return skill_dict(row)

    @router.get("/api/skills/{skill_id}")
    def get_skill(skill_id: str):
        with db.sessions() as session:
            return skill_dict(require(session, Skill, skill_id))

    @router.patch("/api/skills/{skill_id}")
    def patch_skill(skill_id: str, request: SkillPatch):
        with db.sessions.begin() as session:
            row = require(session, Skill, skill_id)
            changes = request.model_dump(exclude_unset=True)
            if "name" in changes:
                unique(session, Skill, changes["name"], row.id)
            for key, value in changes.items():
                setattr(row, key, int(value) if key == "enabled" else value)
            if {"content", "description"} & changes.keys():
                row.version += 1
            emit(session, "skill.updated", None, skill_id=row.id, version=row.version)
            return skill_dict(row)

    @router.delete("/api/skills/{skill_id}", status_code=204)
    def delete_skill(skill_id: str):
        with db.sessions.begin() as session:
            session.delete(require(session, Skill, skill_id))
            emit(session, "skill.deleted", None, skill_id=skill_id)

    @router.get("/api/mcp/servers")
    def list_servers():
        with db.sessions() as session:
            rows = session.scalars(select(McpServer).order_by(McpServer.name))
            return {"enabled": settings.mcp_enabled, "servers": [mcp.public(r) for r in rows]}

    @router.post("/api/mcp/servers", status_code=201)
    def create_server(request: McpServerCreate):
        with db.sessions.begin() as session:
            unique(session, McpServer, request.name)
            row = McpServer(
                name=request.name,
                transport=request.transport,
                config=request.config,
                enabled=int(request.enabled),
                auto_approve_read_only=int(request.auto_approve_read_only),
                tools=[],
                status={},
            )
            session.add(row)
            session.flush()
            emit(session, "mcp.created", None, server_id=row.id, name=row.name)
            return mcp.public(row)

    @router.patch("/api/mcp/servers/{server_id}")
    def patch_server(server_id: str, request: McpServerPatch):
        with db.sessions.begin() as session:
            row = require(session, McpServer, server_id)
            changes = request.model_dump(exclude_unset=True)
            if "name" in changes:
                unique(session, McpServer, changes["name"], row.id)
                row.name = changes["name"]
            if "config" in changes:
                model = McpStdio if row.transport == "stdio" else McpHttp
                config = mcp.keep_secrets(row.config, changes["config"])
                row.config = model.model_validate(config).model_dump(mode="json")
                row.tools, row.status = [], {"ok": None, "note": "config changed; refresh required"}
            for key in ("enabled", "auto_approve_read_only"):
                if key in changes:
                    setattr(row, key, int(changes[key]))
            emit(session, "mcp.updated", None, server_id=row.id)
            return mcp.public(row)

    @router.delete("/api/mcp/servers/{server_id}", status_code=204)
    def delete_server(server_id: str):
        with db.sessions.begin() as session:
            session.delete(require(session, McpServer, server_id))
            emit(session, "mcp.deleted", None, server_id=server_id)

    @router.post("/api/mcp/servers/{server_id}/refresh")
    async def refresh_server(server_id: str):
        if not settings.mcp_enabled:
            raise PermissionError("MCP disabled by kernel policy")
        return await mcp.refresh(db, settings, server_id)

    return router
