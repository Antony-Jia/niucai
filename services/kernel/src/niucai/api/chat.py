"""Unified asynchronous conversation admission and durable timeline projection."""

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from niucai.control.conversations import ConversationManager
from niucai.control.progress import task_snapshot
from niucai.control.tasks import require
from niucai.storage.db import Artifact, Conversation, Event, Message, Task, as_dict


class ConversationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(default="新会话", min_length=1, max_length=200)
    agent_id: str = "main"
    computer_id: str | None = None


class MessageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=12000)
    client_id: str | None = Field(default=None, min_length=1, max_length=100)


class ChatCreate(MessageCreate):
    conversation_id: str | None = None
    computer_id: str | None = None


def chat_router(db, settings, auth, injected_gateway=None):
    router = APIRouter(dependencies=auth)
    manager = ConversationManager(db)

    def response(values):
        conversation, message, task = values
        with db.sessions() as session:
            return {
                "conversation": as_dict(conversation),
                "messages": [as_dict(message)],
                "task": task_snapshot(session, require(session, Task, task.id)),
                "accepted": True,
            }

    @router.get("/api/conversations")
    def conversations(limit: int = Query(100, ge=1, le=200)):
        with db.sessions() as session:
            return [
                as_dict(c)
                for c in session.scalars(
                    select(Conversation)
                    .order_by(Conversation.updated_at.desc(), Conversation.id)
                    .limit(limit)
                )
            ]

    @router.post("/api/conversations", status_code=201)
    def create(request: ConversationCreate):
        with db.sessions.begin() as session:
            return as_dict(manager.create_in(session, **request.model_dump()))

    @router.get("/api/conversations/{conversation_id}/messages")
    def messages(conversation_id: str, limit: int = Query(200, ge=1, le=200)):
        with db.sessions() as session:
            require(session, Conversation, conversation_id)
            rows = list(
                session.scalars(
                    select(Message)
                    .where(Message.conversation_id == conversation_id)
                    .order_by(Message.sequence.desc())
                    .limit(limit)
                )
            )
            return [as_dict(m) for m in reversed(rows)]

    @router.post("/api/conversations/{conversation_id}/messages", status_code=202)
    def send(conversation_id: str, request: MessageCreate):
        return response(manager.send(conversation_id, request.content, request.client_id))

    @router.post("/api/chat", status_code=202)
    def chat(request: ChatCreate):
        return response(
            manager.send(
                request.conversation_id, request.content, request.client_id, computer_id=request.computer_id
            )
        )

    @router.get("/api/conversations/{conversation_id}/timeline")
    def timeline(conversation_id: str, after: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=200)):
        with db.sessions() as session:
            require(session, Conversation, conversation_id)
            task_ids = select(Task.id).where(Task.conversation_id == conversation_id)
            rows = list(
                session.scalars(
                    select(Event)
                    .where(
                        Event.id > after,
                        (Event.task_id.in_(task_ids))
                        | (Event.data["conversation_id"].as_string() == conversation_id),
                    )
                    .order_by(Event.id)
                    .limit(limit + 1)
                )
            )
            items = []
            for event in rows[:limit]:
                item = {
                    "id": f"event:{event.id}",
                    "cursor": event.id,
                    "type": event.type,
                    "task_id": event.task_id,
                    "created_at": event.created_at,
                    "data": event.data,
                }
                if event.type == "conversation.message_created":
                    message = session.get(Message, event.data.get("message_id"))
                    if message:
                        item["message"] = as_dict(message)
                items.append(item)
            tasks = list(
                session.scalars(
                    select(Task).where(Task.conversation_id == conversation_id).order_by(Task.created_at)
                )
            )
            artifacts = session.scalars(select(Artifact).where(Artifact.task_id.in_(task_ids))).all()
            return {
                "items": items,
                "next_cursor": rows[min(len(rows), limit) - 1].id if rows else after,
                "has_more": len(rows) > limit,
                "tasks": [task_snapshot(session, t) for t in tasks],
                "artifacts": [as_dict(a) for a in artifacts],
            }

    return router
