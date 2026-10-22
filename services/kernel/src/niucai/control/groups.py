"""Group routing is a Kernel decision. Display messages never steer other workflows."""

import re

from sqlalchemy import select

from niucai.control.bots import active_agent
from niucai.control.collaboration import CollaborationManager, check_admission
from niucai.control.conversations import ConversationManager, append_message, lock_conversation
from niucai.control.tasks import Conflict
from niucai.storage.db import ConversationParticipant, Message, Task, WorkItem


def send_group(db, settings, ident, content, client_id):
    with db.sessions.begin() as session:
        check_admission(session, settings)
        display = lock_conversation(session, ident)
        members = list(
            session.scalars(
                select(ConversationParticipant)
                .where(ConversationParticipant.conversation_id == ident)
                .order_by(ConversationParticipant.agent_id)
            )
        )
        if not members:
            return None
        if client_id:
            existing = session.scalar(
                select(Message).where(Message.conversation_id == ident, Message.client_id == client_id)
            )
            if existing:
                if existing.content != content:
                    raise Conflict("client_id reused with different content")
                return display, existing, session.get(Task, existing.task_id)
        ids = [m.agent_id for m in members]
        # A mention starts a token, so e-mail addresses such as a@b.com are not routed.
        mentions = re.findall(r"(?<![\w.@])@([a-zA-Z][a-zA-Z0-9_-]*)", content)
        if "everyone" in mentions:
            recipients = ids
        elif mentions:
            if any(ident not in ids for ident in mentions):
                raise Conflict("mentioned Bot is not a group member")
            recipients = list(dict.fromkeys(mentions))
        else:
            recipients = [display.agent_id]
        workflow, first_task, previous = None, None, None
        for agent_id in recipients:
            active_agent(session, agent_id)
            execution = ConversationManager.create_in(session, content[:80], agent_id)
            task = Task(
                title=content[:80],
                goal=content,
                agent_id=agent_id,
                conversation_id=execution.id,
                checkpoint={
                    "runtime": "pi",
                    "session_scope": "conversation",
                    "broadcast_previous_task": previous if "everyone" in mentions else None,
                },
            )
            session.add(task)
            session.flush()
            if workflow is None:
                workflow, item = CollaborationManager(db, settings).root(session, task, dedicated=True)
                workflow.origin_conversation_id = display.id
                # Member operations are authorized by the human group instruction;
                # each member still enforces its own Bot policy.
                workflow.authorization = {"tools": ["*"], "agents": ids, "project_results": True}
                first_task = task
            else:
                item = WorkItem(
                    workflow_id=workflow.id,
                    assignee_agent_id=agent_id,
                    conversation_id=execution.id,
                    task_id=task.id,
                    goal=content,
                )
                session.add(item)
                session.flush()
            append_message(
                session,
                execution,
                role="user",
                source="USER",
                kind="INSTRUCTION",
                content=content,
                task_id=task.id,
                workflow_id=workflow.id,
                work_item_id=item.id,
            )
            previous = task.id
        message = append_message(
            session,
            display,
            role="user",
            content=content,
            client_id=client_id,
            task_id=first_task.id,
            workflow_id=workflow.id,
        )
        # The display input is a projection, already consumed by independent executions.
        message.delivered_at = message.created_at
        return display, message, first_task
