import asyncio
import re

from opentelemetry import trace
from pydantic import TypeAdapter
from sqlalchemy import select

from niucai.control.progress import clear_wait
from niucai.control.tasks import ComputerBusy, ComputerRequired, Conflict, require
from niucai.domain.schemas import ActionSpec
from niucai.storage.db import Action, Approval, Artifact, Audit, Computer, Message, Task, emit, now

tracer = trace.get_tracer("niucai.actions")


class ActionGateway:
    def __init__(self, db, settings, adapter):
        self.db, self.settings, self.adapter = db, settings, adapter

    def propose(self, task_id, token, spec, key, input_sequence=None):
        spec = TypeAdapter(ActionSpec).validate_python(spec).model_dump(mode="json")
        with self.db.sessions.begin() as s:
            from niucai.control.bots import permitted
            from niucai.control.collaboration import gate

            if gate(s, shared=True).stopped:
                raise Conflict("all Bots are stopped")
            task = require(s, Task, task_id, lock=True)
            self.check_task(task, token)
            agent_policy = permitted(s, task, spec["type"])
            if task.checkpoint.get("bot_reply_committed") or task.checkpoint.get("bot_transfer_committed"):
                raise Conflict("work item has already replied")
            existing = s.scalar(select(Action).where(Action.idempotency_key == key))
            if existing:
                if existing.task_id != task_id or existing.spec != spec:
                    raise Conflict("idempotency key reused with a different request")
                return existing
            if input_sequence is not None:
                latest = s.scalar(
                    select(Message.sequence)
                    .where(Message.task_id == task.id, Message.role == "user")
                    .order_by(Message.sequence.desc())
                    .limit(1)
                )
                if latest != input_sequence:
                    raise ValueError("user instructions changed; reconsider the action")
            kind = spec["type"]
            if kind == "shell.exec" and not self.settings.allow_shell:
                raise PermissionError("shell execution disabled by kernel policy")
            if kind.startswith("browser."):
                computer_id = task.computer_id or agent_policy.default_computer_id
                if not computer_id:
                    raise ComputerRequired("请为任务选择执行电脑，然后继续任务。")
                computer = require(s, Computer, computer_id, lock=True)
                if computer.active_task_id and computer.active_task_id != task.id:
                    other = s.get(Task, computer.active_task_id)
                    if other and other.status in {"PENDING", "RUNNING", "PAUSED", "WAITING_HUMAN"}:
                        raise ComputerBusy("执行电脑忙，等待电脑资源释放")
                if computer.control != "AGENT":
                    raise ComputerRequired("请先交还或恢复执行电脑。")
                task.computer_id, computer.active_task_id = computer_id, task.id
            # Semantic side effects cannot be inferred from DOM alone. Conservative V1 policy.
            risk = "HIGH" if kind in {"browser.click", "browser.fill", "files.write", "shell.exec"} else "LOW"
            if kind == "mcp.call":
                from niucai.control.mcp import call_risk

                # 只读且服务器允许自动放行的 MCP 工具为 LOW，其余一律需要审批（或 YOLO）。
                risk = call_risk(s, self.settings, agent_policy, spec)
            elif kind == "skills.read":
                from niucai.control.skills import find

                find(s, agent_policy, spec["name"])
            from niucai.control.autonomy import NEVER_AUTO, yolo

            auto = risk == "HIGH" and kind not in NEVER_AUTO and yolo(s, self.settings, task, "actions")
            action = Action(
                task_id=task.id,
                agent_id=task.agent_id,
                computer_id=task.computer_id,
                idempotency_key=key,
                spec=spec,
                risk=risk,
                status=("APPROVED" if auto else "WAITING_APPROVAL") if risk == "HIGH" else "PROPOSED",
                result={"input_sequence": input_sequence} if input_sequence is not None else {},
            )
            s.add(action)
            s.flush()
            emit(s, "action.proposed", task.id, action_id=action.id, type=kind, risk=risk)
            s.add(Audit(action_id=action.id, outcome="PROPOSED", data={"type": kind, "risk": risk}))
            if risk == "HIGH" and auto:
                # YOLO 仍保留审批记录与审计，只是由会话设置代替人工决定。
                approval = Approval(
                    action_id=action.id, status="APPROVED", note="YOLO 自动批准", decided_at=now()
                )
                s.add(approval)
                s.add(Audit(action_id=action.id, outcome="AUTO_APPROVED", data={"policy": "yolo"}))
                s.flush()
                emit(s, "approval.auto_approved", task.id, approval_id=approval.id, action_id=action.id)
            elif risk == "HIGH":
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
            from niucai.control.collaboration import gate, sync_human_wait

            gate(s, shared=True)
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
            approval_wait = (
                task.status == "WAITING_HUMAN" and task.checkpoint.get("reason") == "WAITING_APPROVAL"
            )
            if approval_wait and task.checkpoint.get("action_id") == action.id:
                task.checkpoint = clear_wait(task.checkpoint)
            unresolved = s.scalar(
                select(Action.id)
                .where(Action.task_id == task.id, Action.status.in_(["WAITING_APPROVAL", "UNKNOWN"]))
                .limit(1)
            )
            if approval_wait and not unresolved:
                task.status = "PENDING"
                emit(s, "task.resumed", task.id, reason="approval_decided")
            sync_human_wait(s, task)
            return approval

    async def execute(self, action_id, token):
        # Journal intent before any side effect. Crash => UNKNOWN, never blind retry.
        with self.db.sessions.begin() as s:
            from niucai.control.bots import permitted
            from niucai.control.collaboration import gate

            if gate(s, shared=True).stopped:
                raise Conflict("all Bots are stopped")
            action = require(s, Action, action_id)
            task = require(s, Task, action.task_id, lock=True)
            self.check_task(task, token)
            permitted(s, task, action.spec["type"])
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
            read_only = action.spec["type"] in {"browser.snapshot", "files.read", "skills.read"} or (
                action.spec["type"] == "mcp.call" and action.risk == "LOW"
            )
            try:
                self.check_task(task, token)
                agent_policy = permitted(s, task, action.spec["type"])
                if task.checkpoint.get("bot_reply_committed") or task.checkpoint.get(
                    "bot_transfer_committed"
                ):
                    raise Conflict("work item has already finished")
                if computer and computer.kind != "linux":
                    raise PermissionError("Android adapter is not available in V1")
                if computer and (computer.control != "AGENT" or computer.active_task_id != task.id):
                    raise Conflict("computer is not leased to this agent task")
                if action.spec["type"] in {"browser.click", "browser.fill"}:
                    ref = action.spec["ref"]
                    if not re.fullmatch(r"e[1-9][0-9]*", ref) or ref not in computer.state.get("refs", []):
                        raise ValueError("invalid or stale browser reference")
                from niucai.control.mcp import KERNEL_ACTIONS, kernel_execute

                with tracer.start_as_current_span("action.execute") as span:
                    span.set_attribute("action.id", action.id)
                    if action.spec["type"] in KERNEL_ACTIONS:
                        # 技能与 MCP 由 Kernel 自身执行，不占用执行电脑适配器。
                        call = kernel_execute(s, self.settings, agent_policy, action.spec)
                        timeout = max(self.settings.action_timeout, self.settings.mcp_timeout + 5)
                    else:
                        call = self.adapter.execute(action.spec, computer)
                        timeout = self.settings.action_timeout
                    dispatched = True
                    result = await asyncio.wait_for(call, timeout)
                action.status, action.result = "SUCCEEDED", result
                if computer and action.spec["type"].startswith("browser."):
                    computer.state = {**computer.state, **result}
                    computer.status = "ONLINE"
                if "path" in result and action.spec["type"] in {"files.write", "browser.screenshot"}:
                    artifact = Artifact(
                        task_id=task.id,
                        path=result["path"],
                        media_type=result.get("media_type", "text/plain"),
                    )
                    if self.settings.multi_bot_enabled:
                        from niucai.control.artifacts import snapshot

                        source = (self.settings.workspace / result["path"]).resolve()
                        if source.is_relative_to(self.settings.workspace.resolve()) and source.is_file():
                            artifact = snapshot(
                                s, self.settings, task.id, source.read_bytes(), artifact.media_type
                            )
                    s.add(artifact)
                    s.flush()
                    emit(
                        s,
                        "artifact.created",
                        task.id,
                        artifact_id=artifact.id,
                        path=artifact.path,
                        media_type=artifact.media_type,
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
