import asyncio
import re

from opentelemetry import trace
from pydantic import TypeAdapter
from sqlalchemy import select

from niucai.control.tasks import Conflict, require
from niucai.domain.schemas import ActionSpec
from niucai.storage.db import Action, Approval, Artifact, Audit, Computer, Task, emit, now

tracer = trace.get_tracer("niucai.actions")


class ActionGateway:
    def __init__(self, db, settings, adapter):
        self.db, self.settings, self.adapter = db, settings, adapter

    def propose(self, task_id, token, spec, key):
        spec = TypeAdapter(ActionSpec).validate_python(spec).model_dump(mode="json")
        with self.db.sessions.begin() as s:
            task = require(s, Task, task_id, lock=True)
            self.check_task(task, token)
            existing = s.scalar(select(Action).where(Action.idempotency_key == key))
            if existing:
                if existing.task_id != task_id or existing.spec != spec:
                    raise Conflict("idempotency key reused with a different request")
                return existing
            kind = spec["type"]
            if kind == "shell.exec" and not self.settings.allow_shell:
                raise PermissionError("shell execution disabled by kernel policy")
            if kind.startswith("browser.") and not task.computer_id:
                raise Conflict("browser action requires a computer")
            # Semantic side effects cannot be inferred from DOM alone. Conservative V1 policy.
            risk = "HIGH" if kind in {"browser.click", "browser.fill", "files.write", "shell.exec"} else "LOW"
            action = Action(
                task_id=task.id,
                agent_id=task.agent_id,
                computer_id=task.computer_id,
                idempotency_key=key,
                spec=spec,
                risk=risk,
                status="WAITING_APPROVAL" if risk == "HIGH" else "PROPOSED",
            )
            s.add(action)
            s.flush()
            emit(s, "action.proposed", task.id, action_id=action.id, type=kind, risk=risk)
            s.add(Audit(action_id=action.id, outcome="PROPOSED", data={"type": kind, "risk": risk}))
            if risk == "HIGH":
                approval = Approval(action_id=action.id)
                s.add(approval)
                s.flush()
                emit(s, "approval.requested", task.id, approval_id=approval.id, action_id=action.id)
            return action

    @staticmethod
    def check_task(task, token):
        if (
            task.status != "RUNNING"
            or task.run_token != token
            or not task.lease_until
            or task.lease_until < now()
        ):
            raise Conflict("task is not owned by this running worker")

    def decide(self, approval_id, approved, note=""):
        with self.db.sessions.begin() as s:
            # Resolve foreign ids, then lock in task -> computer -> action order.
            approval = require(s, Approval, approval_id)
            action = require(s, Action, approval.action_id)
            task = require(s, Task, action.task_id, lock=True)
            action = require(s, Action, action.id, lock=True)
            approval = require(s, Approval, approval_id, lock=True)
            if approval.status != "PENDING":
                raise Conflict("approval already decided")
            if task.status in {"CANCELLED", "COMPLETED", "FAILED"}:
                raise Conflict("task is no longer eligible for approval")
            approval.status, approval.note, approval.decided_at = (
                "APPROVED" if approved else "DENIED",
                note,
                now(),
            )
            action.status = "APPROVED" if approved else "DENIED"
            s.add(Audit(action_id=action.id, outcome=action.status, data={"note": note}))
            emit(s, "action.approved" if approved else "action.denied", task.id, action_id=action.id)
            if task.status == "WAITING_HUMAN":
                task.status = "PENDING"
                emit(s, "task.resumed", task.id, reason="approval_decided")
            return approval

    async def execute(self, action_id, token):
        # Journal intent before any side effect. Crash => UNKNOWN, never blind retry.
        with self.db.sessions.begin() as s:
            action = require(s, Action, action_id)
            task = require(s, Task, action.task_id, lock=True)
            self.check_task(task, token)
            action = require(s, Action, action_id, lock=True)
            if action.status in {"SUCCEEDED", "FAILED", "DENIED", "WAITING_APPROVAL", "UNKNOWN"}:
                return action
            if action.status == "EXECUTING":
                action.status = "UNKNOWN"
                action.result = {"error": "prior execution interrupted; human inspection required"}
                emit(s, "action.unknown", task.id, action_id=action.id)
                s.add(Audit(action_id=action.id, outcome="UNKNOWN", data={}))
                return action
            action.status = "EXECUTING"
            emit(s, "action.executing", task.id, action_id=action.id)
        # Hold task and computer locks while the bounded action executes. Takeover waits
        # for this action, then invalidates the lease before the next action can begin.
        with self.db.sessions.begin() as s:
            action = require(s, Action, action_id)
            task = require(s, Task, action.task_id, lock=True)
            computer = require(s, Computer, action.computer_id, lock=True) if action.computer_id else None
            action = require(s, Action, action_id, lock=True)
            dispatched = False
            read_only = action.spec["type"] in {"browser.snapshot", "files.read"}
            try:
                self.check_task(task, token)
                if computer and computer.kind != "linux":
                    raise PermissionError("Android adapter is not available in V1")
                if computer and (computer.control != "AGENT" or computer.active_task_id != task.id):
                    raise Conflict("computer is not leased to this agent task")
                if action.spec["type"] in {"browser.click", "browser.fill"}:
                    ref = action.spec["ref"]
                    if not re.fullmatch(r"e[1-9][0-9]*", ref) or ref not in computer.state.get("refs", []):
                        raise ValueError("invalid or stale browser reference")
                with tracer.start_as_current_span("action.execute") as span:
                    span.set_attribute("action.id", action.id)
                    dispatched = True
                    result = await asyncio.wait_for(
                        self.adapter.execute(action.spec, computer), self.settings.action_timeout
                    )
                action.status, action.result = "SUCCEEDED", result
                if computer and action.spec["type"].startswith("browser."):
                    computer.state = {**computer.state, **result}
                    computer.status = "ONLINE"
                if "path" in result and action.spec["type"] in {"files.write", "browser.screenshot"}:
                    s.add(
                        Artifact(
                            task_id=task.id,
                            path=result["path"],
                            media_type=result.get("media_type", "text/plain"),
                        )
                    )
            except Conflict:
                # No executor call took place; safe to reconsider after resume.
                action.status = "APPROVED" if action.risk == "HIGH" else "PROPOSED"
                s.commit()
                raise
            except TimeoutError:
                action.status, action.result = (
                    "FAILED" if read_only else "UNKNOWN",
                    {"error": "execution timed out"},
                )
            except asyncio.CancelledError:
                action.status, action.result = "UNKNOWN", {"error": "execution interrupted"}
            except Exception as exc:
                action.status, action.result = (
                    "UNKNOWN" if dispatched and not read_only else "FAILED",
                    {"error": type(exc).__name__, "detail": str(exc)[:500]},
                )
            s.add(Audit(action_id=action.id, outcome=action.status, data={"type": action.spec["type"]}))
            emit(s, f"action.{action.status.lower()}", task.id, action_id=action.id, result=action.result)
            return action
