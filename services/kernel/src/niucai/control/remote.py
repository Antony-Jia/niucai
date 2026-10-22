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

    def create_node(self, name, resource_type="unspecified", connection=None):
        self.enabled()
        token = secrets.token_urlsafe(32)
        with self.db.sessions.begin() as s:
            node = RemoteNode(
                name=name,
                resource_type=resource_type,
                token_hash=sha256(token.encode()).hexdigest(),
                connection=connection or {},
            )
            s.add(node)
            s.flush()
            return {**self.node_view(node), "token": token}

    def node_view(self, node):
        value = as_dict(node)
        value.pop("token_hash")
        value["status"] = (
            "ONLINE"
            if node.heartbeat_at
            and node.heartbeat_at > now() - timedelta(seconds=self.settings.remote_lease_seconds)
            else "OFFLINE"
        )
        value["capacity"] = 1
        value["connection_status"] = value["status"] if node.heartbeat_at else "PENDING"
        protocol = node.capabilities.get("protocol_version")
        value["protocol_status"] = (
            "UNVERIFIED" if protocol is None else "COMPATIBLE" if protocol in {1, 2} else "INCOMPATIBLE"
        )
        return value

    def nodes(self):
        with self.db.sessions.begin() as s:
            scheduling_lock(s)
            self.expire(s)
            occupied = {
                j.node_id: {"id": j.id, "status": j.status}
                for j in s.scalars(select(RemoteJob).where(RemoteJob.status.in_(ACTIVE)))
            }
            return [
                {**self.node_view(n), "active_job": occupied.get(n.id)}
                for n in s.scalars(select(RemoteNode).order_by(RemoteNode.created_at))
            ]

    def update_node(self, node_id, changes):
        self.enabled()
        with self.db.sessions.begin() as s:
            scheduling_lock(s)
            node = require(s, RemoteNode, node_id, lock=True)
            if changes.get("enabled") is False:
                self.require_idle(s, node_id)
            for key in ("name", "resource_type", "enabled", "connection"):
                if key in changes:
                    setattr(node, key, changes[key])
            emit(s, "remote.node.updated", node_id=node_id)
            return self.node_view(node)

    def revoke_node(self, node_id):
        """Emergency revocation keeps LOST/active occupancy until confirmed process exit."""
        with self.db.sessions.begin() as s:
            scheduling_lock(s)
            node = require(s, RemoteNode, node_id, lock=True)
            node.enabled = 0
            for job in s.scalars(select(RemoteJob).where(RemoteJob.node_id == node_id)):
                if job.status == "RUNNING":
                    job.status = "CANCELLING"
                    job_event(s, job, "cancel_requested", reason="node authorization revoked")
            emit(s, "remote.node.revoked", node_id=node_id)
            return self.node_view(node)

    @staticmethod
    def require_idle(s, node_id):
        if s.scalar(
            select(RemoteJob.id).where(RemoteJob.node_id == node_id, RemoteJob.status.in_(ACTIVE)).limit(1)
        ):
            raise Conflict("节点仍有执行中、取消中或 LOST 任务，请先确认原任务退出")

    def rotate_token(self, node_id):
        self.enabled()
        token = secrets.token_urlsafe(32)
        with self.db.sessions.begin() as s:
            scheduling_lock(s)
            node = require(s, RemoteNode, node_id, lock=True)
            self.require_idle(s, node_id)
            node.token_hash = sha256(token.encode()).hexdigest()
            node.heartbeat_at = None
            node.capabilities = {}
            emit(s, "remote.node.token_rotated", node_id=node_id)
            return {**self.node_view(node), "token": token}

    @staticmethod
    def check_credential(node, token_hash):
        if token_hash is not None and not secrets.compare_digest(node.token_hash, token_hash):
            raise PermissionError("node credential was rotated")

    def expire(self, s):
        for job in s.scalars(select(RemoteJob).where(RemoteJob.status.in_(["RUNNING", "CANCELLING"]))):
            if job.lease_until and job.lease_until < now():
                job.status = "LOST"
                job_event(s, job, "lost")
        nodes = list(s.scalars(select(RemoteNode)))
        occupied = {j.node_id for j in s.scalars(select(RemoteJob).where(RemoteJob.status.in_(ACTIVE)))}
        for job in s.scalars(select(RemoteJob).where(RemoteJob.status == "QUEUED")):
            targets = [
                n
                for n in nodes
                if (not job.allowed_nodes or n.id in job.allowed_nodes)
                and (not job.node_id or n.id == job.node_id)
            ]
            online = [
                n
                for n in targets
                if n.enabled
                and n.heartbeat_at
                and n.heartbeat_at > now() - timedelta(seconds=self.settings.remote_lease_seconds)
            ]
            policy = job.config_snapshot.get("resource_policy", {})
            needs_v2 = policy.get("strategy") == "fixed" or bool(job.config_snapshot.get("instructions"))
            compatible = [
                n
                for n in online
                if n.capabilities.get("protocol_version", 1) in {1, 2}
                and (not needs_v2 or n.capabilities.get("protocol_version") == 2)
            ]
            capable = [
                n
                for n in compatible
                if all(
                    c in n.capabilities.get("features", []) for c in policy.get("required_capabilities", [])
                )
            ]
            reason = (
                "NODE_OFFLINE"
                if not online
                else "PROTOCOL_UNVERIFIED_OR_INCOMPATIBLE"
                if not compatible
                else "CAPABILITY_MISSING"
                if not capable
                else "PI_NOT_READY"
                if not any(n.capabilities.get("available") for n in capable)
                else "NODE_BUSY"
                if all(n.id in occupied for n in capable)
                else "QUEUED"
            )
            state = {"reason": reason}
            if reason == "NODE_OFFLINE":
                state["since"] = job.wait_state.get("since") or now().isoformat()
                from datetime import datetime

                timeout = job.config_snapshot.get("resource_policy", {}).get("offline_wait_seconds")
                if timeout and (now() - datetime.fromisoformat(state["since"])).total_seconds() > timeout:
                    job.status, job.result = "FAILED", {"error": "NODE_OFFLINE_TIMEOUT", "recoverable": True}
                    job_event(s, job, "finished", result=job.result)
            if job.wait_state != state:
                job.wait_state = state

    def submit(self, prompt, key, allowed_nodes=(), parent_task_id=None, parent_token=None, artifacts=None):
        self.enabled()
        with self.db.sessions.begin() as s:
            from niucai.control.bots import permitted
            from niucai.control.collaboration import gate

            if gate(s).stopped:
                raise Conflict("all Bots are stopped")
            references = []
            saved = {}
            if parent_task_id:
                task = require(s, Task, parent_task_id, lock=True)
                policy = permitted(s, task, "remote_pi_submit")
                from niucai.control.bots import definition
                from niucai.storage.db import Agent

                current = definition(require(s, Agent, task.agent_id))
                if policy.allowed_nodes == []:
                    raise PermissionError("Bot has no authorized remote nodes")
                # V1 remote CLI cannot enforce a per-tool bridge inside the node.
                if (
                    policy.tools != ["*"]
                    or task.checkpoint.get("authorized_tools", ["*"]) != ["*"]
                    or any(scope != ["*"] for scope in task.checkpoint.get("authorization_chain", []))
                ):
                    raise PermissionError("remote V1 cannot enforce fine-grained tool restrictions")
                if policy.allowed_nodes is not None:
                    if allowed_nodes and not set(allowed_nodes).issubset(policy.allowed_nodes):
                        raise PermissionError("remote node outside Bot whitelist")
                    allowed_nodes = allowed_nodes or policy.allowed_nodes
                target = policy.resource_policy.fixed_node_id
                if target:
                    if allowed_nodes and target not in allowed_nodes:
                        raise PermissionError("fixed Bot cannot select another node")
                    allowed_nodes = [target]
                if current.allowed_nodes is not None:
                    if current.allowed_nodes == []:
                        raise PermissionError("Bot remote node permission revoked")
                    allowed_nodes = [
                        n for n in (allowed_nodes or current.allowed_nodes) if n in current.allowed_nodes
                    ]
                    if not allowed_nodes:
                        raise PermissionError("snapshot nodes no longer authorized")
                saved = policy.model_dump(mode="json")
                if parent_token:
                    from niucai.actions.gateway import ActionGateway

                    ActionGateway.check_task(task, parent_token)
                elif task.status in {"COMPLETED", "CANCELLED", "FAILED", "PAUSED"}:
                    raise Conflict("parent task is not active")
                if artifacts:
                    from niucai.control.collaboration import CollaborationManager
                    from niucai.storage.db import WorkItem

                    item = s.scalar(select(WorkItem).where(WorkItem.task_id == task.id))
                    if not item:
                        raise PermissionError("artifact delegation requires a workflow")
                    references = CollaborationManager(self.db, self.settings).artifact_refs(
                        s, item.workflow_id, artifacts
                    )
            elif artifacts:
                raise PermissionError("artifact delegation requires a parent work item")
            scheduling_lock(s)
            from niucai.control.instance import instance

            if not instance(s).admission_enabled:
                raise Conflict("Kernel is draining for migration")
            existing = s.scalar(select(RemoteJob).where(RemoteJob.idempotency_key == key))
            allowed_nodes = sorted(set(allowed_nodes))
            if existing:
                if existing.input_artifacts != references:
                    raise Conflict("remote input artifact versions changed on replay")
                if (existing.prompt, existing.allowed_nodes, existing.parent_task_id) != (
                    prompt,
                    allowed_nodes,
                    parent_task_id,
                ):
                    raise Conflict("idempotency key reused with a different job")
                return as_dict(existing)
            if parent_task_id and self.settings.multi_bot_enabled:
                from niucai.control.collaboration import CollaborationManager, fail_workflow

                workflow, _ = CollaborationManager(self.db, self.settings).root(s, task)
                if workflow.status != "ACTIVE":
                    raise Conflict("remote job workflow is not active")
                if workflow.handoff_count >= workflow.limits["handoffs"]:
                    fail_workflow(s, workflow, "remote work package hard limit exceeded")
                    return {"status": "FAILED", "error": "remote work package hard limit exceeded"}
                workflow.handoff_count += 1
                workflow.version += 1
            for ident in allowed_nodes:
                require(s, RemoteNode, ident)
            job = RemoteJob(
                prompt=prompt,
                idempotency_key=key,
                allowed_nodes=allowed_nodes,
                parent_task_id=parent_task_id,
                input_artifacts=references,
                config_snapshot=saved,
            )
            s.add(job)
            s.flush()
            job_event(s, job, "submitted", execution_policy="isolated-node-autonomous")
            if parent_task_id:
                from niucai.control.autonomy import yolo

                if yolo(s, self.settings, task, "remote"):
                    # 隔离节点工作包按会话 YOLO 直接入队，事件流仍记录批准来源。
                    job.status = "QUEUED"
                    job_event(s, job, "approve", policy="yolo")
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
            from niucai.control.collaboration import gate

            if gate(s).stopped and operation in {"approve", "retry"}:
                raise Conflict("all Bots are stopped")
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

    def node_heartbeat(self, node_id, capabilities, token_hash=None):
        self.enabled()
        with self.db.sessions.begin() as s:
            scheduling_lock(s)
            node = require(s, RemoteNode, node_id)
            self.check_credential(node, token_hash)
            node.heartbeat_at, node.capabilities = now(), capabilities
            self.expire(s)
            from niucai.control.instance import PROTOCOL_VERSION, instance

            expected = capabilities.get("kernel_instance_id")
            ident = instance(s).instance_id
            return {
                "enabled": bool(node.enabled)
                and (not expected or expected == ident)
                and capabilities.get("protocol_version", 1) in {1, 2},
                "kernel_instance_id": ident,
                "protocol_version": PROTOCOL_VERSION,
                "reason": "KERNEL_IDENTITY_MISMATCH" if expected and expected != ident else None,
            }

    def authorized(self, s, job, node_id):
        """Snapshot pins configuration; current policy can revoke permissions at every boundary."""
        if not job.parent_task_id:
            return True
        from niucai.control.bots import definition, permitted
        from niucai.storage.db import Agent

        parent = require(s, Task, job.parent_task_id)
        try:
            permitted(s, parent, "remote_pi_submit")
            current = definition(require(s, Agent, parent.agent_id))
            return (
                current.tools == ["*"]
                and parent.status in {"PENDING", "RUNNING", "WAITING_HUMAN", "WAITING_BOT"}
                and (current.allowed_nodes is None or node_id in current.allowed_nodes)
            )
        except PermissionError:
            return False

    def claim(self, node_id, token_hash=None):
        self.enabled()
        with self.db.sessions.begin() as s:
            from niucai.control.collaboration import gate

            if gate(s).stopped:
                return None
            scheduling_lock(s)
            self.expire(s)
            node = require(s, RemoteNode, node_id)
            self.check_credential(node, token_hash)
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
            if node.capabilities.get("protocol_version", 1) not in {1, 2}:
                return None
            from niucai.control.instance import instance

            expected = node.capabilities.get("kernel_instance_id")
            if expected and expected != instance(s).instance_id:
                return None
            for job in s.scalars(
                select(RemoteJob).where(RemoteJob.status == "QUEUED").order_by(RemoteJob.created_at)
            ):
                if job.node_id and job.node_id != node_id:
                    continue
                if job.allowed_nodes and node_id not in job.allowed_nodes:
                    continue
                if not self.authorized(s, job, node_id):
                    continue
                policy = job.config_snapshot.get("resource_policy", {})
                if (
                    policy.get("strategy") == "fixed" or job.config_snapshot.get("instructions")
                ) and node.capabilities.get("protocol_version") != 2:
                    # Legacy runners cannot consume role/configuration snapshots.
                    # Keep the basic legacy protocol, never pretend new contracts were enforced.
                    continue
                if any(
                    c not in node.capabilities.get("features", [])
                    for c in policy.get("required_capabilities", [])
                ):
                    continue
                if job.parent_task_id:
                    parent = require(s, Task, job.parent_task_id)
                    occupied = list(
                        s.scalars(
                            select(RemoteJob)
                            .join(Task, RemoteJob.parent_task_id == Task.id)
                            .where(Task.agent_id == parent.agent_id, RemoteJob.status.in_(ACTIVE))
                        )
                    )
                    if len(occupied) >= policy.get("max_concurrency", 32):
                        continue
                if job.parent_task_id:
                    parent = require(s, Task, job.parent_task_id)
                    if parent.status not in {"RUNNING", "WAITING_HUMAN", "WAITING_BOT"}:
                        continue
                job.node_id, job.attempt_id = node_id, uid()
                job.status = "RUNNING"
                job.wait_state = {}
                job.event_sequence = 0
                job.lease_until = now() + timedelta(seconds=self.settings.remote_lease_seconds)
                job_event(s, job, "started", attempt_id=job.attempt_id)
                from niucai.storage.db import WorkItem

                claimed = as_dict(job)
                # Binary uploads are scoped to workflow work items; other jobs return inline files.
                claimed["artifact_transport"] = bool(
                    job.parent_task_id
                    and s.scalar(select(WorkItem.id).where(WorkItem.task_id == job.parent_task_id))
                )
                return claimed
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
                or not self.authorized(s, job, node_id)
                or job.status != "RUNNING"
                or (parent and parent.status not in {"PENDING", "RUNNING", "WAITING_HUMAN", "WAITING_BOT"})
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
            if not self.authorized(s, job, node_id) and job.status == "RUNNING":
                status, result = "FAILED", {"error": "Bot or parent authorization revoked"}
            # LOST/CANCELLING can acknowledge process exit, but cannot publish a success.
            if job.status != "RUNNING":
                status = "CANCELLED" if job.status == "CANCELLING" else "FAILED"
                result = {"error": "execution stopped after cancellation or lease loss"}
            if status == "COMPLETED" and job.parent_task_id and self.settings.multi_bot_enabled:
                import base64

                from niucai.control.artifacts import reference, snapshot
                from niucai.storage.db import WorkItem

                owner = s.scalar(select(WorkItem).where(WorkItem.task_id == job.parent_task_id))
                from niucai.storage.db import Artifact, ArtifactUpload

                outputs = [
                    reference(require(s, Artifact, u.artifact_id))
                    for u in s.scalars(
                        select(ArtifactUpload).where(
                            ArtifactUpload.job_id == job.id,
                            ArtifactUpload.attempt_id == attempt_id,
                            ArtifactUpload.artifact_id.is_not(None),
                        )
                    )
                ]
                budget = 64000
                if owner:
                    for file in result.get("files", [])[:16]:
                        if "base64" in file:
                            content = base64.b64decode(file["base64"], validate=True)
                        elif "text" in file:
                            content = file["text"].encode("utf-8")
                        else:
                            continue
                        if len(content) > min(budget, 16000):
                            raise ValueError("remote artifact exceeds bounded transport budget")
                        budget -= len(content)
                        if file.get("content_hash") and file["content_hash"] != sha256(content).hexdigest():
                            raise Conflict("remote artifact content hash mismatch")
                        artifact = snapshot(
                            s,
                            self.settings,
                            job.parent_task_id,
                            content,
                            file.get("media_type", "text/plain"),
                        )
                        outputs.append(reference(artifact))
                result = {**result, "artifacts": outputs}
            job.status, job.result, job.lease_until = status, result, None
            job_event(s, job, "finished", result=result)
            return as_dict(job)

    def artifact_content(self, node_id, job_id, attempt_id, artifact_id):
        with self.db.sessions() as s:
            job = self.owned(s, node_id, job_id, attempt_id)
            if job.status != "RUNNING" or not job.lease_until or job.lease_until < now():
                raise Conflict("artifact download requires a live owned job")
            ref = next((r for r in job.input_artifacts if r["id"] == artifact_id), None)
            if not ref:
                raise PermissionError("artifact is not an approved input for this job")
            from niucai.storage.db import Artifact

            artifact = require(s, Artifact, artifact_id)
            path = (self.settings.workspace / artifact.path).resolve()
            if not path.is_relative_to(self.settings.workspace.resolve()) or not path.is_file():
                raise Conflict("artifact content unavailable")
            content = path.read_bytes()
            if len(content) > 16000 or sha256(content).hexdigest() != ref["content_hash"]:
                raise Conflict("artifact changed or exceeds remote input size limit")
            return content, artifact.media_type

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
