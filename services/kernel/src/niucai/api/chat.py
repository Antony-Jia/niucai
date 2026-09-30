"""Persisted conversation API. Chat has no tools and never changes task state."""

import asyncio

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from niucai.control.tasks import Conflict, require
from niucai.models.gateway import ModelGateway, ModelRouter
from niucai.storage.db import Conversation, Message, as_dict, emit


class ChatCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=12000)
    conversation_id: str | None = None


def chat_router(db, settings, auth, injected_gateway=None):
    router = APIRouter(dependencies=auth)

    @router.get("/api/conversations")
    def conversations(limit: int = Query(100, ge=1, le=200)):
        with db.sessions() as s:
            return [
                as_dict(c)
                for c in s.scalars(select(Conversation).order_by(Conversation.created_at.desc()).limit(limit))
            ]

    @router.get("/api/conversations/{conversation_id}/messages")
    def messages(conversation_id: str, limit: int = Query(200, ge=1, le=200)):
        with db.sessions() as s:
            require(s, Conversation, conversation_id)
            rows = list(
                s.scalars(
                    select(Message)
                    .where(Message.conversation_id == conversation_id)
                    .order_by(Message.created_at.desc(), Message.id.desc())
                    .limit(limit)
                )
            )
            return [as_dict(m) for m in reversed(rows)]

    @router.post("/api/chat")
    async def chat(request: ChatCreate):
        content = request.content.strip()
        if not content:
            raise Conflict("message must not be blank")
        # Lock the durable conversation row, not a process-local asyncio lock.
        # Reject concurrent turns; PENDING also exposes interrupted requests after restart.
        with db.sessions.begin() as s:
            if request.conversation_id:
                conversation = s.scalar(
                    select(Conversation).where(Conversation.id == request.conversation_id).with_for_update()
                )
                if conversation is None:
                    require(s, Conversation, request.conversation_id)
                pending = s.scalar(
                    select(Message).where(
                        Message.conversation_id == conversation.id, Message.status == "PENDING"
                    )
                )
                if pending:
                    raise Conflict("previous reply is pending; start a new conversation if interrupted")
            else:
                conversation = Conversation(title=content[:80])
                s.add(conversation)
                s.flush()
            user = Message(conversation_id=conversation.id, role="user", content=content)
            assistant = Message(
                conversation_id=conversation.id, role="assistant", content="", status="PENDING"
            )
            s.add(user)
            s.flush()
            s.add(assistant)
            s.flush()
            conversation_data, user_data, assistant_id = as_dict(conversation), as_dict(user), assistant.id
            history = list(
                s.scalars(
                    select(Message)
                    .where(Message.conversation_id == conversation.id, Message.status == "COMPLETED")
                    .order_by(Message.created_at.desc())
                    .limit(12)
                )
            )
            # Keep current turn intact, bound prior history before passing it to the gateway.
            remaining, selected = 24000 - len(content), []
            for message in history:
                if message.id == user.id:
                    continue
                piece = message.content[-min(2000, max(0, remaining)) :]
                if remaining <= 0:
                    break
                selected.append({"role": message.role, "content": piece})
                remaining -= len(piece)
            selected.reverse()
            emit(s, "chat.message_created", conversation_id=conversation.id, message_id=user.id)
        gateway = injected_gateway
        status = "COMPLETED"
        try:
            if gateway is None:
                gateway = ModelGateway(db, settings, ModelRouter(settings.model_config_path))
            result = await asyncio.wait_for(
                gateway.chat(
                    "executor",
                    [
                        {
                            "role": "system",
                            "content": "You are niucai. This is a conversation with no tools. "
                            "Do not claim you executed actions. Ask the user to create a Task for execution.",
                        },
                        *selected,
                        {"role": "user", "content": content},
                    ],
                    conversation_data["id"],
                ),
                timeout=190,
            )
            answer = result["choices"][0]["message"]["content"]
            if not isinstance(answer, str) or not answer.strip():
                raise ValueError("empty model response")
            answer = answer[:64000]
        except Exception:
            status = "FAILED"
            answer = (
                "模型暂时无法回复。请检查服务器的 OpenRouter 密钥、executor 模型配置与网络后重试。"
                "你的消息已保存。"
            )
        finally:
            if gateway is not None and injected_gateway is None:
                await gateway.close()
        with db.sessions.begin() as s:
            assistant = require(s, Message, assistant_id)
            assistant.content, assistant.status = answer, status
            emit(
                s,
                "chat.reply_completed" if status == "COMPLETED" else "chat.reply_failed",
                conversation_id=conversation_data["id"],
                message_id=assistant.id,
            )
            s.flush()
            return {"conversation": conversation_data, "messages": [user_data, as_dict(assistant)]}

    return router
