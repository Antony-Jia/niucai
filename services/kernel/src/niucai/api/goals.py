"""Goal（重构 P3）：用户目标对话 + 本 Goal 白板 + 编排运行记录。"""

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from niucai.control.cards import CardManager
from niucai.control.conversations import append_message, lock_conversation
from niucai.control.orchestrator import enqueue, goal_status, is_goal, search_memories
from niucai.control.tasks import Conflict, Missing, require
from niucai.storage.db import Card, Conversation, Memory, Message, OrchestratorRun, as_dict, emit


class GoalCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(default="", max_length=200)
    message: str = Field(min_length=1, max_length=16000)


class GoalMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    content: str = Field(min_length=1, max_length=16000)
    client_id: str | None = Field(default=None, min_length=1, max_length=100)


class MemoryUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    content: str | None = Field(default=None, min_length=1, max_length=16000)
    tags: list[str] | None = Field(default=None, max_length=20)


MESSAGE_FIELDS = ("id", "sequence", "role", "source", "kind", "content", "created_at")


def run_view(run, full=False):
    value = as_dict(run)
    if not full:
        value["tool_calls"] = len(run.steps)
        value.pop("steps")
        value["output"] = run.output[:500]
    return value


def goals_router(db, settings, auth):
    router = APIRouter()
    cards = CardManager(db, settings)

    def goal(s, goal_id, lock=False):
        conversation = lock_conversation(s, goal_id) if lock else require(s, Conversation, goal_id)
        if not is_goal(conversation):
            raise Missing("goal not found")
        return conversation

    def enabled():
        if not settings.orchestrator_enabled:
            raise Conflict("编排未启用：设置 NIUCAI_ORCHESTRATOR_ENABLED=true 并重启 Kernel 与 Worker")

    @router.get("/api/goals", dependencies=auth)
    def goals(limit: int = Query(100, ge=1, le=200)):
        with db.sessions() as s:
            rows = [
                c
                for c in s.scalars(select(Conversation).order_by(Conversation.updated_at.desc()).limit(500))
                if is_goal(c)
            ][:limit]
            counts = {}
            if rows:
                for conversation_id, status, count in s.execute(
                    select(Card.conversation_id, Card.status, func.count(Card.id))
                    .where(Card.conversation_id.in_([c.id for c in rows]))
                    .group_by(Card.conversation_id, Card.status)
                ):
                    counts.setdefault(conversation_id, {})[status] = count
            return [
                {
                    "id": c.id,
                    "title": c.title,
                    "status": goal_status(c),
                    "cards": counts.get(c.id, {}),
                    "updated_at": c.updated_at,
                    "created_at": c.created_at,
                }
                for c in rows
            ]

    @router.post("/api/goals", dependencies=auth, status_code=201)
    def create(request: GoalCreate):
        enabled()
        with db.sessions.begin() as s:
            conversation = Conversation(
                title=(request.title or request.message.splitlines()[0])[:200],
                settings={"mode": "goal", "goal_status": "ACTIVE"},
            )
            s.add(conversation)
            s.flush()
            message = append_message(
                s, conversation, role="user", source="USER", kind="INSTRUCTION", content=request.message
            )
            enqueue(s, conversation.id, {"kind": "message", "message_id": message.id})
            emit(s, "goal.created", conversation_id=conversation.id)
            return {"id": conversation.id, "title": conversation.title, "status": "ACTIVE"}

    @router.get("/api/goals/{goal_id}", dependencies=auth)
    def detail(goal_id: str):
        with db.sessions() as s:
            conversation = goal(s, goal_id)
            messages = list(
                s.scalars(
                    select(Message)
                    .where(Message.conversation_id == goal_id)
                    .order_by(Message.sequence.desc())
                    .limit(200)
                )
            )[::-1]
            runs = list(
                s.scalars(
                    select(OrchestratorRun)
                    .where(OrchestratorRun.conversation_id == goal_id)
                    .order_by(OrchestratorRun.created_at.desc())
                    .limit(20)
                )
            )
            value = {
                "id": conversation.id,
                "title": conversation.title,
                "status": goal_status(conversation),
                "orchestrator_enabled": settings.orchestrator_enabled,
                "messages": [{k: m.__dict__[k] for k in MESSAGE_FIELDS} for m in messages],
                "runs": [run_view(r) for r in runs],
            }
        value["board"] = cards.board(goal_id)
        return value

    @router.post("/api/goals/{goal_id}/messages", dependencies=auth, status_code=201)
    def send(goal_id: str, request: GoalMessage):
        enabled()
        with db.sessions.begin() as s:
            conversation = goal(s, goal_id, lock=True)
            if request.client_id:
                existing = s.scalar(
                    select(Message).where(
                        Message.conversation_id == goal_id, Message.client_id == request.client_id
                    )
                )
                if existing:
                    return {"message_id": existing.id, "duplicate": True}
            message = append_message(
                s,
                conversation,
                role="user",
                source="USER",
                kind="INSTRUCTION",
                content=request.content,
                client_id=request.client_id,
            )
            if goal_status(conversation) != "ACTIVE":
                conversation.settings = {**conversation.settings, "goal_status": "ACTIVE"}
            enqueue(s, goal_id, {"kind": "message", "message_id": message.id, "body": request.content[:1000]})
            return {"message_id": message.id, "duplicate": False}

    @router.post("/api/goals/{goal_id}/run", dependencies=auth, status_code=202)
    def rerun(goal_id: str):
        enabled()
        with db.sessions.begin() as s:
            goal(s, goal_id, lock=True)
            run = enqueue(s, goal_id, {"kind": "manual"})
            return {"run_id": run.id}

    @router.get("/api/orchestrator/runs/{run_id}", dependencies=auth)
    def run(run_id: str):
        with db.sessions() as s:
            return run_view(require(s, OrchestratorRun, run_id), full=True)

    @router.get("/api/memories/search", dependencies=auth)
    def memory_search(q: str = "", kind: str | None = None, limit: int = Query(50, ge=1, le=200)):
        return search_memories(db, q, kind, limit)

    @router.patch("/api/memories/{memory_id}", dependencies=auth)
    def update_memory(memory_id: str, request: MemoryUpdate):
        with db.sessions.begin() as s:
            memory = require(s, Memory, memory_id, lock=True)
            for key, value in request.model_dump(exclude_unset=True).items():
                if value is None:
                    raise ValueError("updated fields cannot be null")
                setattr(memory, key, value)
            emit(s, "memory.updated", memory_id=memory.id)
            return as_dict(memory)

    @router.delete("/api/memories/{memory_id}", dependencies=auth)
    def delete_memory(memory_id: str):
        with db.sessions.begin() as s:
            s.delete(require(s, Memory, memory_id, lock=True))
            emit(s, "memory.deleted", memory_id=memory_id)
            return {"deleted": memory_id}

    return router
