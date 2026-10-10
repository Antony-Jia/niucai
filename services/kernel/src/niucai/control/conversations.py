"""Durable conversation inbox. The database owns admission, Pi owns reasoning."""

from sqlalchemy import select, update

from niucai.control.tasks import Conflict, require
from niucai.storage.db import Action, Agent, Approval, Audit, Computer, Conversation, Message, Task, emit, now


def lock_conversation(session, ident):
    # UPDATE also serializes SQLite writers, where SELECT FOR UPDATE is a no-op.
    result = session.execute(update(Conversation).where(Conversation.id == ident).values(updated_at=now()))
    if not result.rowcount:
        require(session, Conversation, ident)
    return require(session, Conversation, ident, lock=True)


def append_message(session, conversation, **fields):
    conversation.next_sequence += 1
    message = Message(conversation_id=conversation.id, sequence=conversation.next_sequence, **fields)
    session.add(message)
    session.flush()
    emit(
        session,
        "conversation.message_created",
        message.task_id,
        conversation_id=conversation.id,
        message_id=message.id,
        sequence=message.sequence,
    )
    return message


def invalidate_proposals(session, task):
    for action in session.scalars(
        select(Action).where(
            Action.task_id == task.id, Action.status.in_(["PROPOSED", "WAITING_APPROVAL", "APPROVED"])
        )
    ):
        action.status = "DENIED"
        action.result = {"error": "superseded by a new user message"}
        approval = session.scalar(select(Approval).where(Approval.action_id == action.id))
        if approval and approval.status == "PENDING":
            approval.status, approval.note, approval.decided_at = "DENIED", action.result["error"], now()
        session.add(Audit(action_id=action.id, outcome="DENIED", data={"reason": "user_steering"}))
        emit(session, "action.denied", task.id, action_id=action.id, reason="user_steering")
    task.checkpoint = {k: v for k, v in task.checkpoint.items() if k != "proposal"}


class ConversationManager:
    def __init__(self, db):
        self.db = db

    @staticmethod
    def create_in(session, title="新会话", agent_id="main", computer_id=None):
        require(session, Agent, agent_id)
        if computer_id:
            computer = require(session, Computer, computer_id)
            if computer.kind != "linux":
                raise Conflict("only Linux computers support execution")
        conversation = Conversation(title=title, agent_id=agent_id, computer_id=computer_id)
        session.add(conversation)
        session.flush()
        return conversation

    def send(self, ident, content, client_id=None, *, title=None, computer_id=None, agent_id="main"):
        if not content.strip():
            raise Conflict("message must not be blank")
        with self.db.sessions.begin() as session:
            conversation = (
                lock_conversation(session, ident)
                if ident
                else self.create_in(session, title or content[:80], agent_id, computer_id)
            )
            if client_id:
                existing = session.scalar(
                    select(Message).where(
                        Message.conversation_id == conversation.id, Message.client_id == client_id
                    )
                )
                if existing:
                    if existing.content != content:
                        raise Conflict("client_id reused with different content")
                    return conversation, existing, session.get(Task, existing.task_id)
            task = session.scalar(
                select(Task)
                .where(
                    Task.conversation_id == conversation.id, Task.status.not_in(["COMPLETED", "CANCELLED"])
                )
                .with_for_update()
            )
            if conversation.next_sequence == 0 and conversation.title == "新会话":
                conversation.title = content[:80]
            if task is None:
                task = Task(
                    title=content[:80],
                    goal=content,
                    conversation_id=conversation.id,
                    agent_id=conversation.agent_id,
                    computer_id=conversation.computer_id,
                    checkpoint={"session_scope": "conversation", "runtime": "pi"},
                )
                session.add(task)
                session.flush()
                emit(session, "task.created", task.id, title=task.title)
            else:
                invalidate_proposals(session, task)
            message = append_message(
                session, conversation, role="user", content=content, task_id=task.id, client_id=client_id
            )
            conversation.updated_at = now()
            return conversation, message, task

    def pending(self, task_id):
        with self.db.sessions() as session:
            return list(
                session.scalars(
                    select(Message)
                    .where(Message.task_id == task_id, Message.role == "user", Message.delivered_at.is_(None))
                    .order_by(Message.sequence)
                )
            )

    def inputs(self, task_id):
        with self.db.sessions() as session:
            return list(
                session.scalars(
                    select(Message)
                    .where(Message.task_id == task_id, Message.role == "user")
                    .order_by(Message.sequence)
                )
            )

    def acknowledge(self, task_id, token, message_id, submission_id):
        from niucai.actions.gateway import ActionGateway

        with self.db.sessions.begin() as session:
            task = require(session, Task, task_id, lock=True)
            ActionGateway.check_task(task, token)
            message = require(session, Message, message_id)
            if message.task_id != task_id or message.role != "user":
                raise Conflict("message does not belong to this execution")
            if message.delivered_at:
                if message.submission_id != str(submission_id):
                    raise Conflict("message submission identity changed")
                return
            message.delivered_at, message.submission_id = now(), str(submission_id)
            emit(
                session,
                "conversation.message_delivered",
                task_id,
                conversation_id=task.conversation_id,
                message_id=message.id,
            )
