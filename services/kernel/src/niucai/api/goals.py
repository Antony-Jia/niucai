"""Goal（重构 P3）：用户目标对话 + 本 Goal 白板 + 编排运行记录。"""

import hashlib

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from niucai.control.cards import CardManager
from niucai.control.conversations import append_message, lock_conversation
from niucai.control.orchestrator import enqueue, goal_status, is_goal, search_memories
from niucai.control.removal import remove_conversation
from niucai.control.tasks import Conflict, Missing, require
from niucai.storage.db import (
    Bot,
    Card,
    Conversation,
    GoalRequest,
    Memory,
    Message,
    OrchestratorRun,
    as_dict,
    emit,
)


class GoalCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(default="", max_length=200)
    message: str = Field(min_length=1, max_length=16000)
    # 幂等键：创建成功但响应丢失时，同一 client_id 重试返回已有 Goal，不再重复编排。
    client_id: str | None = Field(default=None, min_length=1, max_length=100)
    # 用户用 @ 指定的助手名称：编排者优先把卡片交给他们。
    bots: list[str] = Field(default_factory=list, max_length=8)


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


def replay(s, client_id, digest):
    """同一 client_id 的重复创建：内容一致返回已有 Goal，内容不同则冲突。"""
    if not client_id:
        return None
    existing = s.get(GoalRequest, client_id)
    if not existing:
        return None
    if existing.content_digest != digest:
        raise Conflict("client_id reused with different content")
    conversation = s.get(Conversation, existing.conversation_id)
    if conversation.deleted_at:
        raise Conflict("该任务已被删除")
    return {
        "id": conversation.id,
        "title": conversation.title,
        "status": goal_status(conversation),
        "duplicate": True,
    }


def open_questions(s, goal_ids):
    """每个 Goal 尚未被回答的 @kernel 提问：最后一条 QUESTION 之后没有任何用户消息。"""
    if not goal_ids:
        return {}
    last_question = {}
    for conversation_id, sequence in s.execute(
        select(Message.conversation_id, func.max(Message.sequence))
        .where(Message.conversation_id.in_(goal_ids), Message.source == "KERNEL", Message.kind == "QUESTION")
        .group_by(Message.conversation_id)
    ):
        last_question[conversation_id] = sequence
    last_user = dict(
        s.execute(
            select(Message.conversation_id, func.max(Message.sequence))
            .where(Message.conversation_id.in_(list(last_question)), Message.source == "USER")
            .group_by(Message.conversation_id)
        ).all()
    )
    open_ids = {
        conversation_id: sequence
        for conversation_id, sequence in last_question.items()
        if (last_user.get(conversation_id) or 0) < sequence
    }
    result = {}
    for conversation_id, sequence in open_ids.items():
        message = s.scalar(
            select(Message).where(Message.conversation_id == conversation_id, Message.sequence == sequence)
        )
        result[conversation_id] = {
            "message_id": message.id,
            "content": message.content[:500],
            "created_at": message.created_at,
        }
    return result


def goals_router(db, settings, auth):
    router = APIRouter()
    cards = CardManager(db, settings)

    def goal(s, goal_id, lock=False):
        conversation = lock_conversation(s, goal_id) if lock else require(s, Conversation, goal_id)
        if not is_goal(conversation) or conversation.deleted_at:
            raise Missing("goal not found")
        return conversation

    def preferred_bots(s, names):
        """@ 指定的助手必须存在且已启用；按名称去重并保持用户给出的顺序。"""
        found = []
        for name in dict.fromkeys(n.strip().lstrip("@") for n in names if n.strip()):
            bot = s.scalar(select(Bot).where(Bot.name == name))
            if not bot:
                raise ValueError(f"没有名为 {name} 的助手")
            if not bot.enabled:
                raise ValueError(f"助手 {name} 已停用")
            found.append(bot.name)
        return found

    def enabled():
        if not settings.orchestrator_enabled:
            raise Conflict("编排未启用：设置 NIUCAI_ORCHESTRATOR_ENABLED=true 并重启 Kernel 与 Worker")

    @router.get("/api/goals", dependencies=auth)
    def goals(limit: int = Query(100, ge=1, le=200)):
        with db.sessions() as s:
            rows = [
                c
                for c in s.scalars(
                    select(Conversation)
                    .where(Conversation.deleted_at.is_(None))
                    .order_by(Conversation.updated_at.desc())
                    .limit(500)
                )
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
            questions = open_questions(s, [c.id for c in rows if goal_status(c) != "DONE"])
            return [
                {
                    "id": c.id,
                    "title": c.title,
                    "status": goal_status(c),
                    "cards": counts.get(c.id, {}),
                    "question": questions.get(c.id),
                    "updated_at": c.updated_at,
                    "created_at": c.created_at,
                }
                for c in rows
            ]

    @router.post("/api/goals", dependencies=auth, status_code=201)
    def create(request: GoalCreate):
        enabled()
        digest = hashlib.sha256(request.message.encode()).hexdigest()
        try:
            with db.sessions.begin() as s:
                replayed = replay(s, request.client_id, digest)
                if replayed:
                    return replayed
                chosen = preferred_bots(s, request.bots)
                conversation = Conversation(
                    title=(request.title or request.message.splitlines()[0])[:200],
                    settings={
                        "mode": "goal",
                        "goal_status": "ACTIVE",
                        **({"preferred_bots": chosen} if chosen else {}),
                    },
                )
                s.add(conversation)
                s.flush()
                if request.client_id:
                    # 幂等键以主键落库：并发的重复创建在这里只有一个能成功。
                    s.add(
                        GoalRequest(
                            client_id=request.client_id,
                            conversation_id=conversation.id,
                            content_digest=digest,
                        )
                    )
                    s.flush()
                message = append_message(
                    s,
                    conversation,
                    role="user",
                    source="USER",
                    kind="INSTRUCTION",
                    content=request.message,
                    client_id=request.client_id,
                )
                enqueue(s, conversation.id, {"kind": "message", "message_id": message.id})
                emit(s, "goal.created", conversation_id=conversation.id)
                return {"id": conversation.id, "title": conversation.title, "status": "ACTIVE"}
        except IntegrityError:
            # 另一个请求抢先创建了同一个 client_id：回放已有 Goal。
            if not request.client_id:
                raise
            with db.sessions() as s:
                replayed = replay(s, request.client_id, digest)
            if replayed:
                return replayed
            raise

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
                "question": (
                    open_questions(s, [goal_id]).get(goal_id) if goal_status(conversation) != "DONE" else None
                ),
                "messages": [{k: m.__dict__[k] for k in MESSAGE_FIELDS} for m in messages],
                "runs": [run_view(r) for r in runs],
            }
        value["board"] = cards.board(goal_id)
        return value

    @router.delete("/api/goals/{goal_id}", dependencies=auth)
    def delete(goal_id: str):
        with db.sessions.begin() as s:
            goal(s, goal_id)
            remove_conversation(s, goal_id)
            return {"deleted": goal_id}

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
