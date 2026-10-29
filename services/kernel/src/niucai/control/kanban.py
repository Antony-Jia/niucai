"""Read-only Kanban projection: active tasks, the Agents backing them and what each node is busy with."""

from datetime import timedelta

from sqlalchemy import select

from niucai.control.remote import ACTIVE as JOB_ACTIVE
from niucai.control.remote import RemoteManager
from niucai.storage.db import (
    Agent,
    Computer,
    ConversationParticipant,
    Handoff,
    RemoteJob,
    Task,
    WorkItem,
    now,
)

DONE_WINDOW = timedelta(hours=24)
DONE = ("COMPLETED", "FAILED", "CANCELLED")
COLUMNS = ("PENDING", "RUNNING", "WAITING_HUMAN", "WAITING_BOT", "PAUSED", "DONE")


def _brief(text, limit=120):
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "..."


def kanban(db, settings):
    nodes = RemoteManager(db, settings).nodes()  # Expires stale leases before we read job state.
    node_names = {n["id"]: n["name"] for n in nodes}
    with db.sessions() as s:
        names = {a.id: a.name for a in s.scalars(select(Agent))}
        horizon = now() - DONE_WINDOW
        tasks = list(
            s.scalars(
                select(Task)
                .where(Task.status.not_in(DONE) | (Task.updated_at >= horizon))
                .order_by(Task.updated_at.desc())
                .limit(200)
            )
        )
        ids = [t.id for t in tasks]
        titles = {t.id: t.title for t in tasks}
        by_conversation = {t.conversation_id: t.id for t in tasks if t.conversation_id}
        agents = {t.id: {} for t in tasks}

        def add(task_id, agent_id, role):
            if task_id in agents and agent_id:
                agents[task_id].setdefault(agent_id, role)

        for t in tasks:
            add(t.id, t.agent_id, "lead")
        for item in s.scalars(select(WorkItem).where(WorkItem.task_id.in_(ids))):
            add(item.task_id, item.assignee_agent_id, "assignee")
        for p in s.scalars(
            select(ConversationParticipant).where(
                ConversationParticipant.conversation_id.in_(by_conversation)
            )
        ):
            add(by_conversation.get(p.conversation_id), p.agent_id, p.role.lower())
        for h in s.scalars(
            select(Handoff).where(Handoff.to_task_id.in_(ids) | Handoff.from_task_id.in_(ids))
        ):
            add(h.to_task_id, h.to_agent_id, "delegate")
            add(h.from_task_id, h.from_agent_id, "delegator")

        jobs = {}
        for j in s.scalars(
            select(RemoteJob).where(RemoteJob.parent_task_id.in_(ids)).order_by(RemoteJob.created_at)
        ):
            jobs.setdefault(j.parent_task_id, []).append(j)
        cards = [
            {
                "id": t.id,
                "title": t.title,
                "status": t.status,
                "column": "DONE" if t.status in DONE else t.status,
                "conversation_id": t.conversation_id,
                "computer_id": t.computer_id,
                "updated_at": t.updated_at.isoformat() + "Z",
                "agents": [
                    {"id": a, "name": names.get(a, a), "role": role} for a, role in agents[t.id].items()
                ],
                "remote_jobs": [
                    {
                        "id": j.id,
                        "engine": j.engine,
                        "status": j.status,
                        "node_id": j.node_id,
                        "node_name": node_names.get(j.node_id),
                    }
                    for j in jobs.get(t.id, [])
                ],
            }
            for t in tasks
        ]

        busy = {
            j.node_id: j
            for j in s.scalars(
                select(RemoteJob).where(RemoteJob.status.in_(JOB_ACTIVE), RemoteJob.node_id.is_not(None))
            )
        }
        parents = {
            t.id: t.title
            for t in s.scalars(
                select(Task).where(Task.id.in_([j.parent_task_id for j in busy.values() if j.parent_task_id]))
            )
        }
        resources = []
        for n in nodes:
            job = busy.get(n["id"])
            resources.append(
                {
                    "id": n["id"],
                    "name": n["name"],
                    "kind": "node",
                    "resource_type": n.get("resource_type"),
                    "status": n["status"] if n.get("enabled", 1) else "DISABLED",
                    "busy": job is not None,
                    "job": job
                    and {
                        "id": job.id,
                        "engine": job.engine,
                        "status": job.status,
                        "prompt": _brief(job.prompt),
                        "task_id": job.parent_task_id,
                        "task_title": parents.get(job.parent_task_id),
                    },
                }
            )
        for c in s.scalars(select(Computer)):
            resources.append(
                {
                    "id": c.id,
                    "name": c.name,
                    "kind": "computer",
                    "resource_type": c.kind,
                    "status": c.status,
                    "busy": c.active_task_id is not None,
                    "job": c.active_task_id
                    and {
                        "task_id": c.active_task_id,
                        "task_title": titles.get(c.active_task_id),
                        "control": c.control,
                    },
                }
            )
    return {"columns": list(COLUMNS), "tasks": cards, "resources": resources}
