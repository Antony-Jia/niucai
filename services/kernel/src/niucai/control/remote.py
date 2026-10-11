"""Remote scheduling. A lease expiry never implies that external effects stopped."""

import secrets
from datetime import timedelta
from hashlib import sha256

from sqlalchemy import select, text

from niucai.control.tasks import Conflict, require
from niucai.storage.db import Event, RemoteJob, RemoteNode, Task, as_dict, emit, now, uid

ACTIVE = {"RUNNING", "CANCELLING", "LOST"}
TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "DENIED"}


def scheduling_lock(s):
    # One short transaction for admission/control; no network or execution under this lock.
    if s.bind.dialect.name == "postgresql":
        s.execute(text("SELECT pg_advisory_xact_lock(68194211)"))


def job_event(s, job, event_name, **data):
    # Remote events deliberately do not acquire parent Task locks (lock order).
    emit(
        s,
        f"remote.job.{event_name}",
        job_id=job.id,
        parent_task_id=job.parent_task_id,
        node_id=job.node_id,
        status=job.status,
        **data,
    )


def cancel_children(s, task_id):
    scheduling_lock(s)
    for job in s.scalars(select(RemoteJob).where(RemoteJob.parent_task_id == task_id)):
        if job.status in TERMINAL:
            continue
        job.status = "CANCELLING" if job.status in ACTIVE else "CANCELLED"
        job_event(s, job, "cancel_requested")


class RemoteManager:
    def __init__(self, db, settings):
        self.db, self.settings = db, settings

    def enabled(self):
        if not self.settings.remote_pi_enabled:
            raise Conflict("remote Pi execution is disabled")

    def create_node(self, name):
        self.enabled()
        token = secrets.token_urlsafe(32)
        with self.db.sessions.begin() as s:
            node = RemoteNode(name=name, token_hash=sha256(token.encode()).hexdigest())
            s.add(node)
            s.flush()
            return {**self.node_view(node), "token": token}

    def node_view(self, node):
        value = as_dict(node)
        value.pop("token_hash")
        value["status"] = (
            "ONLINE"
            if node.enabled
            and node.heartbeat_at
            and node.heartbeat_at > now() - timedelta(seconds=self.settings.remote_lease_seconds)
            else "OFFLINE"
        )
        value["capacity"] = 1
        return value

    def nodes(self):
        with self.db.sessions() as s:
            return [self.node_view(n) for n in s.scalars(select(RemoteNode).order_by(RemoteNode.created_at))]

    def expire(self, s):
        for job in s.scalars(select(RemoteJob).where(RemoteJob.status.in_(["RUNNING", "CANCELLING"]))):
            if job.lease_until and job.lease_until < now():
                job.status = "LOST"
                job_event(s, job, "lost")

    def submit(self, prompt, key, allowed_nodes=(), parent_task_id=None, parent_token=None):
        self.enabled()
        with self.db.sessions.begin() as s:
            if parent_task_id:
                task = require(s, Task, parent_task_id, lock=True)
                if parent_token:
                    from niucai.actions.gateway import ActionGateway

                    ActionGateway.check_task(task, parent_token)
                elif task.status in {"COMPLETED", "CANCELLED", "FAILED", "PAUSED"}:
                    raise Conflict("parent task is not active")
            scheduling_lock(s)
            existing = s.scalar(select(RemoteJob).where(RemoteJob.idempotency_key == key))
            allowed_nodes = sorted(set(allowed_nodes))
            if existing:
                if (existing.prompt, existing.allowed_nodes, existing.parent_task_id) != (
                    prompt,
                    allowed_nodes,
                    parent_task_id,
                ):
                    raise Conflict("idempotency key reused with a different job")
                return as_dict(existing)
            for ident in allowed_nodes:
                require(s, RemoteNode, ident)
            job = RemoteJob(
                prompt=prompt, idempotency_key=key, allowed_nodes=allowed_nodes, parent_task_id=parent_task_id
            )
            s.add(job)
            s.flush()
            job_event(s, job, "submitted", execution_policy="isolated-node-autonomous")
            return as_dict(job)

    def jobs(self, parent_task_id=None):
        with self.db.sessions.begin() as s:
            scheduling_lock(s)
            self.expire(s)
            query = select(RemoteJob).order_by(RemoteJob.created_at.desc()).limit(200)
            if parent_task_id:
                query = query.where(RemoteJob.parent_task_id == parent_task_id)
            return [as_dict(j) for j in s.scalars(query)]

    def get(self, job_id):
        with self.db.sessions.begin() as s:
            scheduling_lock(s)
            self.expire(s)
            return as_dict(require(s, RemoteJob, job_id))

    def control(self, job_id, operation):
        self.enabled()
        with self.db.sessions.begin() as s:
            scheduling_lock(s)
            job = require(s, RemoteJob, job_id)
            if operation in {"approve", "deny"}:
                if job.status != "WAITING_APPROVAL":
                    raise Conflict("job is not awaiting approval")
                job.status = "QUEUED" if operation == "approve" else "DENIED"
            elif operation == "cancel":
                if job.status in TERMINAL:
                    raise Conflict("job already ended")
                job.status = "CANCELLING" if job.status in ACTIVE else "CANCELLED"
            elif operation == "retry":
                if job.status not in {"FAILED", "CANCELLED"}:
                    raise Conflict("retry requires confirmed process exit; LOST jobs cannot be retried")
                # The previous task's environment/session stays pinned to its node.
                job.status = "WAITING_APPROVAL"
                job.result = {}
            job_event(s, job, operation)
            return as_dict(job)

    def node_heartbeat(self, node_id, capabilities):
        self.enabled()
        with self.db.sessions.begin() as s:
            scheduling_lock(s)
            node = require(s, RemoteNode, node_id)
            node.heartbeat_at, node.capabilities = now(), capabilities
            self.expire(s)
            return {"enabled": bool(node.enabled)}

    def claim(self, node_id):
        self.enabled()
        with self.db.sessions.begin() as s:
            scheduling_lock(s)
            self.expire(s)
            node = require(s, RemoteNode, node_id)
            if (
                not node.enabled
                or not node.heartbeat_at
                or node.heartbeat_at < (now() - timedelta(seconds=self.settings.remote_lease_seconds))
            ):
                return None
            if s.scalar(
                select(RemoteJob.id)
                .where(RemoteJob.node_id == node_id, RemoteJob.status.in_(ACTIVE))
                .limit(1)
            ):
                return None
            if node.capabilities.get("executor") != "pi" or not node.capabilities.get("available"):
                return None
            for job in s.scalars(
                select(RemoteJob).where(RemoteJob.status == "QUEUED").order_by(RemoteJob.created_at)
            ):
                if job.node_id and job.node_id != node_id:
                    continue
                if job.allowed_nodes and node_id not in job.allowed_nodes:
                    continue
                if job.parent_task_id:
                    parent = require(s, Task, job.parent_task_id)
                    if parent.status not in {"RUNNING", "WAITING_HUMAN"}:
                        continue
                job.node_id, job.attempt_id = node_id, uid()
                job.status = "RUNNING"
                job.event_sequence = 0
                job.lease_until = now() + timedelta(seconds=self.settings.remote_lease_seconds)
                job_event(s, job, "started", attempt_id=job.attempt_id)
                return as_dict(job)
            return None

    @staticmethod
    def owned(s, node_id, job_id, attempt_id):
        job = require(s, RemoteJob, job_id)
        if job.node_id != node_id or job.attempt_id != attempt_id:
            raise Conflict("stale or foreign execution attempt")
        return job

    def heartbeat(self, node_id, job_id, attempt_id):
        with self.db.sessions.begin() as s:
            scheduling_lock(s)
            self.expire(s)
            job = self.owned(s, node_id, job_id, attempt_id)
            parent = s.get(Task, job.parent_task_id) if job.parent_task_id else None
            node = require(s, RemoteNode, node_id)
            stop = (
                not self.settings.remote_pi_enabled
                or not node.enabled
                or job.status != "RUNNING"
                or (parent and parent.status not in {"PENDING", "RUNNING", "WAITING_HUMAN"})
            )
            if not stop:
                job.lease_until = now() + timedelta(seconds=self.settings.remote_lease_seconds)
            return {
                "stop": bool(stop),
                "status": job.status,
                "lease_seconds": self.settings.remote_lease_seconds,
            }

    def event(self, node_id, job_id, attempt_id, sequence, kind, data):
        with self.db.sessions.begin() as s:
            scheduling_lock(s)
            self.expire(s)
            job = self.owned(s, node_id, job_id, attempt_id)
            if job.status != "RUNNING":
                raise Conflict("job is not running")
            if sequence <= job.event_sequence:
                return {"accepted": False}
            if sequence != job.event_sequence + 1:
                raise Conflict("event sequence gap")
            job.event_sequence = sequence
            if kind == "session":
                job.session_id = str(data.get("session_id", ""))[:200]
            job_event(s, job, "progress", sequence=sequence, kind=kind, payload=data)
            return {"accepted": True}

    def finish(self, node_id, job_id, attempt_id, status, result):
        with self.db.sessions.begin() as s:
            scheduling_lock(s)
            self.expire(s)
            job = self.owned(s, node_id, job_id, attempt_id)
            if job.status in TERMINAL:
                return as_dict(job)  # Lost HTTP response: do not publish twice.
            if job.status not in ACTIVE:
                raise Conflict("job is not owned by an execution attempt")
            # LOST/CANCELLING can acknowledge process exit, but cannot publish a success.
            if job.status != "RUNNING":
                status = "CANCELLED" if job.status == "CANCELLING" else "FAILED"
                result = {"error": "execution stopped after cancellation or lease loss"}
            job.status, job.result, job.lease_until = status, result, None
            job_event(s, job, "finished", result=result)
            return as_dict(job)

    def events(self, job_id, after):
        with self.db.sessions() as s:
            require(s, RemoteJob, job_id)
            # Filter in SQL, retaining the global event cursor used by existing clients.
            return [
                as_dict(e)
                for e in s.scalars(
                    select(Event)
                    .where(
                        Event.id > after,
                        Event.type.like("remote.job.%"),
                        Event.data["job_id"].as_string() == job_id,
                    )
                    .order_by(Event.id)
                    .limit(200)
                )
            ]
