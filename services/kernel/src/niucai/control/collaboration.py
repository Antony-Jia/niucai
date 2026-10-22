"""Durable collaboration. All collaboration transactions take the global gate first.

The gate serializes short admission/result transactions, never model/network work.
Execution identity and authorization always come from the leased Task.
"""

from datetime import timedelta
from hashlib import sha256

from sqlalchemy import select, update

from niucai.control.autonomy import inherit
from niucai.control.bots import active_agent, definition, permitted, task_definition
from niucai.control.conversations import ConversationManager, append_message, lock_conversation
from niucai.control.tasks import Conflict, require
from niucai.domain.schemas import pipeline_order
from niucai.storage.db import (
    Action,
    Agent,
    Artifact,
    BotControl,
    Computer,
    Conversation,
    Handoff,
    Message,
    Task,
    TaskRun,
    Workflow,
    WorkItem,
    as_dict,
    emit,
    now,
)

DONE = {"REPLIED", "COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"}
FINISHED = {"COMPLETED", "FAILED", "CANCELLED"}
# 上游结论注入下游阶段时的单段上限；完整结论仍在上游阶段会话中。
STAGE_RESULT_CHARS = 8000


def gate(session, shared=False):
    """Exclusive for collaboration state changes (send/wait/reply/wake/stop).
    Shared for hot paths that only need a stable stop flag; they still conflict with
    exclusive holders, but not with each other. A shared holder must never upgrade."""
    if not shared:
        session.execute(update(BotControl).where(BotControl.id == "global").values(id="global"))
    control = session.scalar(select(BotControl).where(BotControl.id == "global").with_for_update(read=shared))
    if control is None:
        control = BotControl(id="global", stopped=0)
        session.add(control)
        session.flush()
    return control


def check_admission(session, settings, new_work=True):
    control = gate(session)
    if control.stopped:
        raise Conflict("all Bots are stopped")
    if not settings.multi_bot_enabled:
        raise Conflict("multi Bot collaboration is disabled")
    from niucai.control.instance import instance

    if new_work and not instance(session).admission_enabled:
        raise Conflict("Kernel is draining for migration")


def release(session, task):
    if task.computer_id:
        computer = require(session, Computer, task.computer_id, lock=True)
        if computer.active_task_id == task.id:
            computer.active_task_id = None


def user_revision(session, task, expected):
    if expected is None:
        return  # Compatibility for administrative/direct calls, never for new Pi intents.
    latest = (
        session.scalar(
            select(Message.sequence)
            .where(
                Message.task_id == task.id,
                Message.role == "user",
                Message.conversation_id == task.conversation_id,
            )
            .order_by(Message.sequence.desc())
            .limit(1)
        )
        or 0
    )
    if latest != expected:
        raise ValueError("user instructions changed; reconsider collaboration intent")


def sync_human_wait(session, task):
    """Account for human waiting at the state transition, not at a later timer tick.

    Callers hold the task lock. Shared gate holders only mutate this task's incoming
    handoff row, and never upgrade the gate or lock another task.
    """
    instant = now()
    for handoff in session.scalars(
        select(Handoff).where(Handoff.to_task_id == task.id, Handoff.status.not_in(DONE)).with_for_update()
    ):
        if task.status == "WAITING_HUMAN":
            if handoff.human_wait_started_at is None:
                handoff.human_wait_started_at = instant
        elif task.status != "PAUSED" and handoff.human_wait_started_at is not None:
            handoff.deadline += max(instant - handoff.human_wait_started_at, timedelta())
            handoff.human_wait_started_at = None


def settle_task(session, task, status):
    if task.run_token:
        run = session.get(TaskRun, task.run_token)
        if run:
            run.status, run.ended_at = status, now()
    task.status, task.run_token, task.lease_until = status, None, None
    sync_human_wait(session, task)
    release(session, task)


def wake(session, item):
    if not item.wait:
        return False
    if "depends_on" in item.wait:
        return wake_stage(session, item)
    workflow = require(session, Workflow, item.workflow_id)
    task = require(session, Task, item.task_id)
    rows = [require(session, Handoff, ident) for ident in item.wait["handoff_ids"]]
    ready = (
        all(h.status in DONE for h in rows)
        if item.wait["mode"] == "all"
        else any(h.status in DONE for h in rows)
    )
    blocked = session.scalar(
        select(Action.id)
        .where(Action.task_id == task.id, Action.status.in_(["WAITING_APPROVAL", "UNKNOWN"]))
        .limit(1)
    )
    if ready and task.status == "WAITING_BOT" and workflow.status == "ACTIVE" and not blocked:
        if not gate(session).stopped:
            task = require(session, Task, item.task_id, lock=True)
            if task.status != "WAITING_BOT":
                return ready
            task.status, item.status = "PENDING", "QUEUED"
            emit(session, "task.resumed", task.id, reason="bot dependencies settled")
    return ready


def wake_stage(session, item):
    """依赖流程阶段：上游全部完成才放行；任一上游失败/取消则级联取消本阶段。"""
    if item.status in FINISHED:
        return False
    workflow = require(session, Workflow, item.workflow_id)
    upstream = [require(session, WorkItem, ident) for ident in item.wait["depends_on"]]
    if any(up.status in {"FAILED", "CANCELLED"} for up in upstream):
        emit(
            session,
            "work_item.blocked",
            item.task_id,
            workflow_id=workflow.id,
            work_item_id=item.id,
            reason="upstream stage did not complete",
        )
        cancel_items(session, workflow, [item])
        return False
    if not all(up.status == "COMPLETED" for up in upstream):
        return False
    task = require(session, Task, item.task_id)
    if task.status != "WAITING_BOT" or workflow.status != "ACTIVE" or gate(session).stopped:
        return True
    task = require(session, Task, item.task_id, lock=True)
    if task.status == "WAITING_BOT":
        release_stage(session, workflow, item, task, upstream)
    return True


def release_stage(session, workflow, item, task, upstream):
    """把上游结论与产物引用合成为本阶段的唯一任务输入，然后放行执行。"""
    keys = {stage["work_item_id"]: stage["key"] for stage in workflow.limits.get("stages", [])}
    artifacts, sections = list(item.artifacts), []
    for up in upstream:
        content = str(up.result.get("content", "")).strip() or "（无文本结论）"
        if len(content) > STAGE_RESULT_CHARS:
            content = content[:STAGE_RESULT_CHARS] + "\n……（已截断，完整结论见上游阶段会话）"
        lines = [f"## 上游阶段 {keys.get(up.id, up.id)}（Bot {up.assignee_agent_id}）", content]
        for ref in up.artifacts:
            if ref not in artifacts:
                artifacts.append(ref)
            artifact = session.get(Artifact, ref["id"])
            path = artifact.path if artifact else "?"
            lines.append(f"- 产物 {ref['id']}（{ref['media_type']}，路径 {path}）")
        sections.append("\n".join(lines))
    sections.append(f"## 本阶段任务（{keys.get(item.id, item.id)}）\n{item.goal}")
    if item.output_requirements:
        sections.append(f"## 输出要求\n{item.output_requirements}")
    if artifacts:
        sections.append(
            "上游产物可用 files.read 按路径读取，或作为 remote_pi_submit 的 artifacts 交给远端节点。"
        )
    incoming = session.scalar(select(Handoff).where(Handoff.to_work_item_id == item.id))
    if incoming:
        # 等待上游期间不计入本阶段的答复期限：从放行时刻重新计时。
        incoming.deadline = now() + (incoming.deadline - incoming.created_at)
    append_message(
        session,
        lock_conversation(session, item.conversation_id),
        role="bot",
        source="BOT",
        kind="ASSIGNMENT",
        content="\n\n".join(sections),
        task_id=task.id,
        sender_agent_id=incoming.from_agent_id if incoming else None,
        handoff_id=incoming.id if incoming else None,
        workflow_id=workflow.id,
        work_item_id=item.id,
    )
    item.artifacts, item.wait, item.version = artifacts, {}, item.version + 1
    task.status, item.status = "PENDING", "QUEUED"
    emit(
        session,
        "work_item.unblocked",
        task.id,
        workflow_id=workflow.id,
        work_item_id=item.id,
        depends_on=[up.id for up in upstream],
    )
    emit(session, "task.resumed", task.id, reason="pipeline dependencies settled")


def create_stages(session, settings, workflow, stages, parent=None, parent_task=None, key=""):
    """建立依赖流程阶段：每个阶段独立会话与执行，有依赖的阶段以 WAITING_BOT 挂起。

    parent/parent_task 为发起流程的 Bot 执行（bot_pipeline）；每个阶段对其形成一条
    DELEGATE handoff，发起者可用 bot_wait 等待。REST 创建的流程没有发起执行。
    """
    order = pipeline_order(stages)
    used = {stage["key"] for stage in workflow.limits.get("stages", [])}
    if used & {stage.key for stage in order}:
        raise Conflict("pipeline stage key already used in this workflow")
    allowed = workflow.authorization.get("agents")
    causal = hop = None
    if parent is not None:
        causal = session.scalar(select(Handoff).where(Handoff.to_work_item_id == parent.id))
        hop = causal.hop + 1 if causal else 1
        if hop > settings.handoff_max_hops:
            raise Conflict("handoff hop limit exceeded")
        if workflow.handoff_count + len(order) > workflow.limits["handoffs"]:
            raise Conflict("workflow handoff hard limit exceeded")
    source = parent_task.conversation_id if parent_task else workflow.origin_conversation_id
    items, records = {}, []
    for stage in order:
        receiver = active_agent(session, stage.agent_id)
        if allowed and stage.agent_id not in allowed:
            raise PermissionError("stage Bot is outside the group authorization")
        title = f"[{stage.key}] {stage.goal}"[:80]
        conversation = ConversationManager.create_in(session, title, receiver.id)
        inherit(session, source, conversation)
        depends = [items[name].id for name in stage.depends_on]
        checkpoint = {"runtime": "pi", "session_scope": "conversation"}
        if parent_task is not None:
            # 与 bot_send 相同：阶段权限不超过发起者与流程授权。
            checkpoint["authorized_tools"] = workflow.authorization["tools"]
            checkpoint["authorization_chain"] = [
                *parent_task.checkpoint.get("authorization_chain", []),
                definition(require(session, Agent, parent_task.agent_id)).tools,
            ]
        task = Task(
            title=title,
            goal=stage.goal,
            agent_id=receiver.id,
            conversation_id=conversation.id,
            status="WAITING_BOT" if depends else "PENDING",
            checkpoint=checkpoint,
        )
        session.add(task)
        session.flush()
        item = WorkItem(
            workflow_id=workflow.id,
            parent_work_item_id=parent.id if parent else None,
            assignee_agent_id=receiver.id,
            definition_snapshot=definition(receiver).model_dump(mode="json"),
            conversation_id=conversation.id,
            task_id=task.id,
            goal=stage.goal,
            output_requirements=stage.output_requirements,
            wait={"depends_on": depends, "mode": "all"} if depends else {},
            status="WAITING_BOT" if depends else "QUEUED",
        )
        session.add(item)
        session.flush()
        items[stage.key] = item
        emit(session, "work_item.created", task.id, workflow_id=workflow.id, work_item_id=item.id)
        handoff = None
        if parent is not None:
            handoff = Handoff(
                workflow_id=workflow.id,
                from_work_item_id=parent.id,
                to_work_item_id=item.id,
                from_agent_id=parent_task.agent_id,
                to_agent_id=receiver.id,
                from_conversation_id=parent_task.conversation_id,
                to_conversation_id=conversation.id,
                from_task_id=parent_task.id,
                to_task_id=task.id,
                caused_by_handoff_id=causal.id if causal else None,
                kind="DELEGATE",
                expects_reply=1,
                hop=hop,
                idempotency_key=f"{key}:{stage.key}",
                payload={
                    "to": receiver.id,
                    "content": stage.goal,
                    "kind": "DELEGATE",
                    "expects_reply": True,
                    "stage": stage.key,
                    "depends_on": stage.depends_on,
                    "output_requirements": stage.output_requirements,
                },
                deadline=now() + timedelta(seconds=settings.handoff_timeout_seconds),
            )
            session.add(handoff)
            session.flush()
            workflow.handoff_count += 1
            for event in ("sent", "accepted"):
                emit(
                    session,
                    f"handoff.{event}",
                    parent_task.id,
                    handoff_id=handoff.id,
                    workflow_id=workflow.id,
                    work_item_id=item.id,
                )
        if not depends:
            content = stage.goal
            if stage.output_requirements:
                content += f"\n\n## 输出要求\n{stage.output_requirements}"
            append_message(
                session,
                conversation,
                role="bot" if parent_task else "user",
                source="BOT" if parent_task else "USER",
                kind="ASSIGNMENT",
                content=content,
                task_id=task.id,
                sender_agent_id=parent_task.agent_id if parent_task else None,
                handoff_id=handoff.id if handoff else None,
                workflow_id=workflow.id,
                work_item_id=item.id,
            )
        records.append(
            {
                "key": stage.key,
                "agent_id": receiver.id,
                "work_item_id": item.id,
                "task_id": task.id,
                "conversation_id": conversation.id,
                "handoff_id": handoff.id if handoff else None,
                "depends_on": stage.depends_on,
            }
        )
    # 阶段图随流程持久化，供界面展示与结论注入时引用阶段名。
    workflow.limits = {**workflow.limits, "stages": [*workflow.limits.get("stages", []), *records]}
    workflow.version += 1
    emit(session, "workflow.pipeline_created", workflow_id=workflow.id, stages=[r["key"] for r in records])
    return records


def conclude(session, workflow):
    """所有工作项结束后结算流程状态。"""
    outstanding = session.scalar(
        select(WorkItem.id)
        .where(WorkItem.workflow_id == workflow.id, WorkItem.status.not_in(FINISHED))
        .limit(1)
    )
    if outstanding or workflow.status != "ACTIVE":
        return
    failures = session.scalar(
        select(WorkItem.id).where(WorkItem.workflow_id == workflow.id, WorkItem.status == "FAILED").limit(1)
    )
    workflow.status = "FAILED" if failures else "COMPLETED"
    final_agents = workflow.limits.get("final_agent_ids", [])
    if final_agents:
        stage = final_stage(session, workflow)
        if stage is None or stage.status != "COMPLETED" or stage.assignee_agent_id not in final_agents:
            workflow.status = "FAILED"
    workflow.version += 1
    emit(session, f"workflow.{workflow.status.lower()}", workflow_id=workflow.id)


def lock_order(item):
    return item.conversation_id or ""


def cancel_items(session, workflow, items, status="CANCELLED"):
    from niucai.control.remote import cancel_children

    ids = {item.id for item in items}
    locked = {
        item.task_id: require(session, Task, item.task_id, lock=True)
        for item in sorted(items, key=lock_order)
        if item.status not in FINISHED
    }
    for item in sorted(items, key=lock_order):
        if item.status not in FINISHED:
            item.status, item.version = status, item.version + 1
            task = locked[item.task_id]
            settle_task(session, task, status)
            cancel_children(session, task.id)
            emit(session, "work_item.cancelled", task.id, work_item_id=item.id, workflow_id=workflow.id)
    for handoff in session.scalars(select(Handoff).where(Handoff.workflow_id == workflow.id)):
        if handoff.to_work_item_id in ids and handoff.status not in DONE:
            handoff.status, handoff.reply = status, {"error": status.lower()}
            emit(session, f"handoff.{status.lower()}", handoff.from_task_id, handoff_id=handoff.id)
    session.flush()
    for item in list(session.scalars(select(WorkItem).where(WorkItem.workflow_id == workflow.id))):
        wake(session, item)


def final_stage(session, workflow):
    """Follow the TRANSFER chain from the root; DELEGATE branches never decide the outcome.
    A TRANSFER completes its source item, so each stage transfers at most once."""
    stage = session.scalar(
        select(WorkItem)
        .where(WorkItem.workflow_id == workflow.id, WorkItem.parent_work_item_id.is_(None))
        .order_by(WorkItem.created_at)
        .limit(1)
    )
    while stage is not None:
        transfer = session.scalar(
            select(Handoff).where(Handoff.from_work_item_id == stage.id, Handoff.kind == "TRANSFER").limit(1)
        )
        if transfer is None:
            return stage
        stage = session.get(WorkItem, transfer.to_work_item_id)
    return None


def fail_workflow(session, workflow, reason):
    workflow.status, workflow.version = "FAILED", workflow.version + 1
    cancel_items(
        session, workflow, list(session.scalars(select(WorkItem).where(WorkItem.workflow_id == workflow.id)))
    )
    emit(session, "workflow.failed", workflow_id=workflow.id, reason=reason)


class CollaborationManager:
    def __init__(self, db, settings):
        self.db, self.settings = db, settings

    def root(self, session, task, dedicated=False):
        """Root item for task. Only dedicated execution conversations are owned by the item;
        a root started from an ordinary chat leaves that conversation open for later rounds."""
        item = session.scalar(select(WorkItem).where(WorkItem.task_id == task.id))
        if item:
            return require(session, Workflow, item.workflow_id, lock=True), item
        workflow = Workflow(
            origin_conversation_id=task.conversation_id,
            owner_agent_id=task.agent_id,
            authorization={"tools": definition(require(session, Agent, task.agent_id)).tools},
            limits={
                "handoffs": self.settings.handoff_max_per_workflow,
                "model_turns": self.settings.workflow_max_model_turns,
                "seconds": self.settings.workflow_max_seconds,
                "revisions": 3,
            },
            model_turns=task.checkpoint.get("collaboration_model_turns", 0),
        )
        session.add(workflow)
        session.flush()
        item = WorkItem(
            workflow_id=workflow.id,
            assignee_agent_id=task.agent_id,
            definition_snapshot=definition(require(session, Agent, task.agent_id)).model_dump(mode="json"),
            conversation_id=task.conversation_id if dedicated else None,
            task_id=task.id,
            goal=task.goal,
            status="RUNNING" if task.status == "RUNNING" else "QUEUED",
        )
        session.add(item)
        session.flush()
        emit(session, "workflow.created", task.id, workflow_id=workflow.id)
        emit(session, "work_item.created", task.id, workflow_id=workflow.id, work_item_id=item.id)
        return workflow, item

    def send(
        self,
        task_id,
        token,
        tool_id,
        to,
        content,
        kind="DELEGATE",
        expects_reply=True,
        stage_output=None,
        output_requirements="",
        attachments=None,
        input_sequence=None,
    ):
        from niucai.actions.gateway import ActionGateway

        if kind not in {"DELEGATE", "TRANSFER"}:
            raise ValueError("invalid handoff kind")
        if kind == "TRANSFER" and (expects_reply or stage_output is None or not output_requirements):
            raise ValueError("TRANSFER requires expects_reply=false, stage_output and output_requirements")
        if not content.strip() or len(content) > 16000:
            raise ValueError("invalid handoff content")
        key = f"{task_id}:pi:{sha256(str(tool_id).encode()).hexdigest()}"
        payload = {
            "to": to,
            "content": content,
            "kind": kind,
            "expects_reply": expects_reply,
            "stage_output": stage_output,
            "output_requirements": output_requirements,
            "attachments": attachments or [],
        }
        with self.db.sessions.begin() as session:
            check_admission(session, self.settings)
            task = require(session, Task, task_id, lock=True)
            ActionGateway.check_task(task, token)
            sender = permitted(session, task, "bot_send")
            current_sender = definition(require(session, Agent, task.agent_id))
            if any(p.delegates is not None and to not in p.delegates for p in (sender, current_sender)):
                raise PermissionError("delegation outside Bot policy")
            existing = session.scalar(select(Handoff).where(Handoff.idempotency_key == key))
            if existing:
                if existing.payload != payload or existing.from_task_id != task_id:
                    raise Conflict("handoff idempotency payload changed")
                return as_dict(existing)
            user_revision(session, task, input_sequence)
            receiver = active_agent(session, to)
            workflow, parent = self.root(session, task)
            if workflow.status != "ACTIVE":
                raise Conflict("workflow is not active")
            if (now() - workflow.created_at).total_seconds() > workflow.limits["seconds"]:
                fail_workflow(session, workflow, "workflow time hard limit exceeded")
                return {"status": "FAILED", "error": "workflow time hard limit exceeded"}
            if workflow.authorization.get("agents") and to not in workflow.authorization["agents"]:
                raise PermissionError("receiver is outside the group authorization")
            if kind == "TRANSFER":
                if [task.agent_id, to] not in workflow.routes:
                    raise PermissionError("stage transfer is outside the configured routes")
                visited = session.scalar(
                    select(WorkItem.id)
                    .where(WorkItem.workflow_id == workflow.id, WorkItem.assignee_agent_id == to)
                    .limit(1)
                )
                if visited:
                    if workflow.revision_count >= workflow.limits["revisions"]:
                        fail_workflow(session, workflow, "stage revision hard limit exceeded")
                        return {"status": "FAILED", "error": "stage revision hard limit exceeded"}
                    workflow.revision_count += 1
            references = self.artifact_refs(session, workflow.id, payload["attachments"])
            causal = session.scalar(select(Handoff).where(Handoff.to_work_item_id == parent.id))
            hop = causal.hop + 1 if causal else 1
            if hop > self.settings.handoff_max_hops:
                raise Conflict("handoff hop limit exceeded")
            if workflow.handoff_count >= workflow.limits["handoffs"]:
                fail_workflow(session, workflow, "handoff hard limit exceeded")
                return {"status": "FAILED", "error": "workflow handoff hard limit exceeded"}
            recent = list(
                session.scalars(
                    select(Handoff).where(
                        Handoff.from_agent_id == task.agent_id,
                        Handoff.to_agent_id == to,
                        Handoff.created_at > now() - timedelta(seconds=60),
                    )
                )
            )
            if len(recent) >= 10:
                raise Conflict("Bot pair rate limit exceeded")
            # Carry each policy independently: wildcards are intersected at execution time.
            conversation = ConversationManager.create_in(session, content[:80], receiver.id)
            inherit(session, task.conversation_id, conversation)
            child = Task(
                title=content[:80],
                goal=content,
                agent_id=receiver.id,
                conversation_id=conversation.id,
                checkpoint={
                    "runtime": "pi",
                    "session_scope": "conversation",
                    "authorized_tools": workflow.authorization["tools"],
                    "authorization_chain": [
                        *task.checkpoint.get("authorization_chain", []),
                        definition(require(session, Agent, task.agent_id)).tools,
                    ],
                },
            )
            session.add(child)
            session.flush()
            item = WorkItem(
                workflow_id=workflow.id,
                parent_work_item_id=parent.id,
                assignee_agent_id=to,
                definition_snapshot=definition(receiver).model_dump(mode="json"),
                conversation_id=conversation.id,
                task_id=child.id,
                goal=content,
                output_requirements=output_requirements,
                artifacts=references,
            )
            session.add(item)
            session.flush()
            emit(session, "work_item.created", child.id, workflow_id=workflow.id, work_item_id=item.id)
            handoff = Handoff(
                workflow_id=workflow.id,
                from_work_item_id=parent.id,
                to_work_item_id=item.id,
                from_agent_id=task.agent_id,
                to_agent_id=to,
                from_conversation_id=task.conversation_id,
                to_conversation_id=conversation.id,
                from_task_id=task.id,
                to_task_id=child.id,
                caused_by_handoff_id=causal.id if causal else None,
                kind=kind,
                expects_reply=int(expects_reply),
                hop=hop,
                idempotency_key=key,
                payload=payload,
                deadline=now() + timedelta(seconds=self.settings.handoff_timeout_seconds),
            )
            session.add(handoff)
            session.flush()
            workflow.handoff_count += 1
            workflow.version += 1
            append_message(
                session,
                conversation,
                role="bot",
                source="BOT",
                kind="ASSIGNMENT",
                content=content,
                task_id=child.id,
                sender_agent_id=task.agent_id,
                handoff_id=handoff.id,
                workflow_id=workflow.id,
                work_item_id=item.id,
            )
            for event in ("sent", "accepted"):
                emit(
                    session,
                    f"handoff.{event}",
                    task.id,
                    handoff_id=handoff.id,
                    workflow_id=workflow.id,
                    work_item_id=item.id,
                )
            if kind == "TRANSFER":
                parent.artifacts = references
                self.finish_in(session, task, parent, "COMPLETED", stage_output)
                task.checkpoint = {**task.checkpoint, "bot_transfer_committed": True, "result": stage_output}
                append_message(
                    session,
                    require(session, Conversation, task.conversation_id),
                    role="assistant",
                    task_id=task.id,
                    content=stage_output,
                )
                settle_task(session, task, "COMPLETED")
                emit(session, "task.completed", task.id)
                return {**as_dict(handoff), "complete": stage_output}
            return as_dict(handoff)

    def pipeline(self, task_id, token, tool_id, stages, input_sequence=None):
        """bot_pipeline：当前 Bot 一次性拆出带依赖的阶段，返回各阶段 handoff 供 bot_wait。"""
        from niucai.actions.gateway import ActionGateway
        from niucai.domain.schemas import PipelineStage

        stages = [PipelineStage.model_validate(stage) for stage in stages]
        pipeline_order(stages)
        key = f"{task_id}:pipeline:{sha256(str(tool_id).encode()).hexdigest()}"
        with self.db.sessions.begin() as session:
            check_admission(session, self.settings)
            task = require(session, Task, task_id, lock=True)
            ActionGateway.check_task(task, token)
            sender = permitted(session, task, "bot_pipeline")
            current_sender = definition(require(session, Agent, task.agent_id))
            for stage in stages:
                policies = (sender, current_sender)
                if any(p.delegates is not None and stage.agent_id not in p.delegates for p in policies):
                    raise PermissionError("delegation outside Bot policy")
            workflow, parent = self.root(session, task)
            existing = [
                record
                for record in workflow.limits.get("stages", [])
                if record.get("handoff_id")
                and (handoff := session.get(Handoff, record["handoff_id"]))
                and handoff.idempotency_key.startswith(f"{key}:")
            ]
            if existing:
                if sorted(r["key"] for r in existing) != sorted(s.key for s in stages):
                    raise Conflict("pipeline idempotency payload changed")
                return {"workflow_id": workflow.id, "stages": existing}
            user_revision(session, task, input_sequence)
            if workflow.status != "ACTIVE":
                raise Conflict("workflow is not active")
            if (now() - workflow.created_at).total_seconds() > workflow.limits["seconds"]:
                fail_workflow(session, workflow, "workflow time hard limit exceeded")
                return {"status": "FAILED", "error": "workflow time hard limit exceeded"}
            records = create_stages(session, self.settings, workflow, stages, parent, task, key)
            return {
                "workflow_id": workflow.id,
                "stages": records,
                "handoff_ids": [r["handoff_id"] for r in records],
                "hint": "Use bot_wait on handoff_ids; downstream stages start after upstream ones complete.",
            }

    def artifact_refs(self, session, workflow_id, references):
        result = []
        for reference in references:
            if set(reference) != {"id", "version", "content_hash", "media_type"}:
                raise ValueError("artifact reference requires id, version, content_hash and media_type")
            artifact = require(session, Artifact, reference["id"])
            owner = session.scalar(select(WorkItem).where(WorkItem.task_id == artifact.task_id))
            if not owner or owner.workflow_id != workflow_id:
                raise PermissionError("artifact outside authorized workflow")
            path = (self.settings.workspace / artifact.path).resolve()
            if not path.is_relative_to(self.settings.workspace.resolve()) or not path.is_file():
                raise Conflict("artifact unavailable in controlled storage")
            digest = sha256(path.read_bytes()).hexdigest()
            if (artifact.version, digest, artifact.media_type) != (
                reference["version"],
                reference["content_hash"],
                reference["media_type"],
            ):
                raise Conflict("artifact version/content conflict")
            artifact.content_hash = digest
            result.append(reference)
        return result

    def wait(self, task_id, token, tool_id, handoff_ids, mode="all"):
        from niucai.actions.gateway import ActionGateway

        if mode not in {"all", "any"} or not handoff_ids or len(set(handoff_ids)) != len(handoff_ids):
            raise ValueError("invalid wait condition")
        with self.db.sessions.begin() as session:
            check_admission(session, self.settings, new_work=False)
            task = require(session, Task, task_id, lock=True)
            ActionGateway.check_task(task, token)
            permitted(session, task, "bot_wait")
            workflow, item = self.root(session, task)
            rows = [require(session, Handoff, ident) for ident in handoff_ids]
            if any(h.from_work_item_id != item.id for h in rows):
                raise PermissionError("cannot wait on another work item's handoff")
            condition = {"handoff_ids": handoff_ids, "mode": mode, "tool_id": str(tool_id)}
            if item.wait.get("tool_id") == str(tool_id) and item.wait != condition:
                raise Conflict("wait replay payload changed")
            item.wait = condition
            if wake(session, item):
                # Durable bot_wait returns these results on every replay. Do not also
                # submit them as independent steering rounds after the wait resumes.
                for message in session.scalars(
                    select(Message).where(
                        Message.task_id == task.id,
                        Message.kind == "RESULT",
                        Message.handoff_id.in_(handoff_ids),
                    )
                ):
                    message.delivered_at = now()
                    message.submission_id = f"bot-wait:{tool_id}"
                return {"results": [as_dict(h) for h in rows if h.status in DONE]}
            return {"wait": {"reason": "bot dependencies", "bot_wait": condition}, "status": "WAITING_BOT"}

    def reply(self, task_id, token, handoff_id, content, attachments=None, input_sequence=None):
        from niucai.actions.gateway import ActionGateway

        with self.db.sessions.begin() as session:
            check_admission(session, self.settings, new_work=False)
            task = require(session, Task, task_id, lock=True)
            ActionGateway.check_task(task, token)
            permitted(session, task, "bot_reply")
            user_revision(session, task, input_sequence)
            sync_human_wait(session, task)
            handoff = require(session, Handoff, handoff_id, lock=True)
            if handoff.to_task_id != task.id or handoff.to_agent_id != task.agent_id:
                raise PermissionError("only the leased receiver can reply")
            if handoff.status in DONE:
                if (
                    handoff.status == "REPLIED"
                    and handoff.reply.get("content") == content
                    and handoff.reply.get("artifacts", []) == (attachments or [])
                ):
                    return {"complete": content}
                raise Conflict("handoff already settled")
            if handoff.deadline < now():
                handoff.status, handoff.reply = "TIMED_OUT", {"error": "late reply rejected"}
                cancel_items(
                    session,
                    require(session, Workflow, handoff.workflow_id),
                    [require(session, WorkItem, handoff.to_work_item_id)],
                )
                return {"status": "TIMED_OUT", "error": "late reply rejected"}
            task.checkpoint = {**task.checkpoint, "result": content}
            # Commit result and revoke the lease together. The worker then exits Pi.
            task.checkpoint = {**task.checkpoint, "bot_reply_committed": True}
            item = require(session, WorkItem, handoff.to_work_item_id)
            item.artifacts = self.artifact_refs(session, item.workflow_id, attachments or [])
            self.finish_in(session, task, item, "COMPLETED", content)
            append_message(
                session,
                require(session, Conversation, task.conversation_id),
                role="assistant",
                task_id=task.id,
                content=content,
            )
            settle_task(session, task, item.status)
            emit(session, f"task.{item.status.lower()}", task.id)
            return {"complete": content, "status": item.status}

    def finish_in(self, session, task, item, status, content):
        if status == "COMPLETED":
            contract = task_definition(session, task).output_contract
            delivered = list(session.scalars(select(Artifact).where(Artifact.task_id == task.id)))
            if (contract.require_text and not content.strip()) or len(delivered) < contract.min_files:
                status = "FAILED"
                content = "OUTPUT_CONTRACT_MISSING: required text or files were not delivered"
                task.status = "FAILED"
                task.checkpoint = {**task.checkpoint, "reason": content, "detail": content}
            if delivered:
                from niucai.control.artifacts import reference

                item.artifacts = [reference(a) for a in delivered]
        item.status, item.result, item.version = status, {"content": content}, item.version + 1
        emit(
            session,
            f"work_item.{status.lower()}",
            task.id,
            workflow_id=item.workflow_id,
            work_item_id=item.id,
        )
        workflow = require(session, Workflow, item.workflow_id)
        handoff = session.scalar(select(Handoff).where(Handoff.to_work_item_id == item.id))
        origins = set()
        if handoff:
            origins.add(handoff.from_conversation_id)
        if workflow.authorization.get("project_results"):
            origins.add(workflow.origin_conversation_id)
        for ident in sorted(origins):
            lock_conversation(session, ident)
        if (
            workflow.authorization.get("project_results")
            and workflow.origin_conversation_id != task.conversation_id
        ):
            # Only final text/status is projected; approval/login payloads stay in execution.
            display = lock_conversation(session, workflow.origin_conversation_id)
            append_message(
                session,
                display,
                role="bot",
                source="BOT",
                kind="RESULT",
                content=content,
                sender_agent_id=task.agent_id,
                workflow_id=workflow.id,
                work_item_id=item.id,
            )
        if handoff and handoff.status not in DONE:
            handoff.status = (
                ("REPLIED" if handoff.expects_reply else "COMPLETED") if status == "COMPLETED" else status
            )
            handoff.reply = {"content": content, "status": status, "artifacts": item.artifacts}
            origin = lock_conversation(session, handoff.from_conversation_id)
            parent_task = require(session, Task, handoff.from_task_id)
            # Results are data. They never create a new execution or invalidate approvals.
            append_message(
                session,
                origin,
                role="bot",
                source="BOT",
                kind="RESULT",
                content=content,
                task_id=parent_task.id,
                sender_agent_id=task.agent_id,
                handoff_id=handoff.id,
                workflow_id=item.workflow_id,
                work_item_id=item.id,
            )
            emit(
                session,
                f"handoff.{handoff.status.lower()}",
                parent_task.id,
                handoff_id=handoff.id,
                workflow_id=item.workflow_id,
            )
        session.flush()
        workflow = require(session, Workflow, item.workflow_id)
        for waiting in list(session.scalars(select(WorkItem).where(WorkItem.workflow_id == workflow.id))):
            wake(session, waiting)
        conclude(session, workflow)

    def tick(self):
        with self.db.sessions.begin() as session:
            if gate(session).stopped:
                return
            for workflow in session.scalars(select(Workflow).where(Workflow.status == "ACTIVE")):
                if (now() - workflow.created_at).total_seconds() > workflow.limits["seconds"]:
                    fail_workflow(session, workflow, "workflow time hard limit exceeded")
            for handoff in session.scalars(
                select(Handoff).where(Handoff.status.not_in(DONE), Handoff.deadline < now())
            ):
                if session.get(WorkItem, handoff.to_work_item_id).wait.get("depends_on"):
                    continue  # 阶段仍在等待上游，放行时才开始计时。
                receiver = session.get(Task, handoff.to_task_id)
                if receiver and (
                    receiver.status == "WAITING_HUMAN"
                    or (receiver.status == "PAUSED" and handoff.human_wait_started_at is not None)
                ):
                    # Keep the original deadline; resume atomically adds the actual wait.
                    # Workflow wall-clock limits still include all human waiting.
                    sync_human_wait(session, receiver)
                    continue
                item = require(session, WorkItem, handoff.to_work_item_id)
                workflow = require(session, Workflow, handoff.workflow_id)
                handoff.status, handoff.reply = "TIMED_OUT", {"error": "handoff deadline exceeded"}
                cancel_items(session, workflow, [item])
                wake(session, require(session, WorkItem, handoff.from_work_item_id))
                emit(session, "handoff.timed_out", handoff.from_task_id, handoff_id=handoff.id)
            for waiting in list(session.scalars(select(WorkItem).where(WorkItem.status == "WAITING_BOT"))):
                wake(session, waiting)
            # 依赖流程可能因级联取消而全部结束，没有执行会再触发结算。
            for workflow in list(session.scalars(select(Workflow).where(Workflow.status == "ACTIVE"))):
                if workflow.limits.get("stages"):
                    conclude(session, workflow)

    def model_turn(self, task_id, token):
        from niucai.actions.gateway import ActionGateway

        with self.db.sessions.begin() as session:
            if gate(session).stopped:
                raise Conflict("all Bots are stopped")
            task = require(session, Task, task_id, lock=True)
            ActionGateway.check_task(task, token)
            item = session.scalar(select(WorkItem).where(WorkItem.task_id == task_id))
            if item:
                workflow = require(session, Workflow, item.workflow_id)
                if workflow.status != "ACTIVE":
                    raise Conflict("workflow is not active")
                if workflow.model_turns >= workflow.limits["model_turns"]:
                    fail_workflow(session, workflow, "model turn hard limit exceeded")
                    return False
                if (now() - workflow.created_at).total_seconds() > workflow.limits["seconds"]:
                    fail_workflow(session, workflow, "workflow time hard limit exceeded")
                    return False
                workflow.model_turns += 1
            task.checkpoint = {
                **task.checkpoint,
                "collaboration_model_turns": task.checkpoint.get("collaboration_model_turns", 0) + 1,
            }
            return True
