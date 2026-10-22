from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import Field, ValidationError
from sqlalchemy import select

from niucai.control.autonomy import inherit
from niucai.control.bots import active_agent, archive, definition, validate_definition
from niucai.control.collaboration import (
    FINISHED,
    cancel_items,
    check_admission,
    gate,
    lock_order,
    settle_task,
    sync_human_wait,
    wake,
)
from niucai.control.tasks import Conflict, require
from niucai.domain.schemas import AgentCreate, AgentDefinition, PipelineStage, Strict, pipeline_order
from niucai.storage.db import (
    Action,
    Agent,
    Conversation,
    ConversationParticipant,
    Handoff,
    RemoteJob,
    Routine,
    RoutineRun,
    Task,
    TaskRun,
    Workflow,
    WorkItem,
    as_dict,
    emit,
)


class AgentPatch(Strict):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    definition: AgentDefinition | None = None


class RoutineCreate(Strict):
    agent_id: str
    conversation_id: str | None = None
    schedule: str = Field(max_length=100)
    timezone: str = "Asia/Shanghai"
    prompt: str = Field(min_length=1, max_length=16000)
    enabled: bool = True


class RoutinePatch(Strict):
    agent_id: str | None = None
    conversation_id: str | None = None
    schedule: str | None = None
    timezone: str | None = None
    prompt: str | None = None
    enabled: bool | None = None


class BoardPatch(Strict):
    expected_version: int
    labels: list[str] = Field(default_factory=list, max_length=20)
    due_at: datetime | None = None


class Participants(Strict):
    agent_ids: list[str] = Field(min_length=2, max_length=6)
    coordinator: str


class WorkflowCreate(Strict):
    agent_id: str = "main"
    goal: str = Field(min_length=1, max_length=16000)
    origin_conversation_id: str | None = None
    routes: list[tuple[str, str]] = Field(default_factory=list, max_length=30)
    final_agent_ids: list[str] = Field(default_factory=list, max_length=6)
    max_revisions: int = Field(default=3, ge=0, le=20)
    participant_agent_ids: list[str] | None = Field(default=None, max_length=32)
    output_requirements: str = Field(default="", max_length=4000)
    # 依赖流程：提供 stages 时不启动协调者执行，按依赖图直接创建各阶段。
    stages: list[PipelineStage] | None = Field(default=None, max_length=12)


def item_dict(session, item):
    # Roots started from an ordinary chat execute in that chat.
    task = session.get(Task, item.task_id) if item.task_id else None
    jobs = list(session.scalars(select(RemoteJob).where(RemoteJob.parent_task_id == item.task_id)))
    return {
        **as_dict(item),
        "conversation_id": item.conversation_id or (task and task.conversation_id),
        "remote_jobs": [
            {
                "id": j.id,
                "node_id": j.node_id,
                "status": j.status,
                "wait_state": j.wait_state,
                "attempt_id": j.attempt_id,
            }
            for j in jobs
        ],
    }


def bot_router(db, settings, auth):
    router = APIRouter(dependencies=auth)

    @router.get("/api/collaboration/status")
    def status():
        with db.sessions.begin() as session:
            local = list(
                session.scalars(
                    select(TaskRun.id).where(
                        TaskRun.process_exited_at.is_(None), TaskRun.exit_tracking_started_at.is_not(None)
                    )
                )
            )
            legacy = list(
                session.scalars(
                    select(TaskRun.id).where(
                        TaskRun.process_exited_at.is_(None), TaskRun.exit_tracking_started_at.is_(None)
                    )
                )
            )
            remote = list(
                session.scalars(
                    select(RemoteJob.id).where(RemoteJob.status.in_(["RUNNING", "CANCELLING", "LOST"]))
                )
            )
            return {
                "enabled": settings.multi_bot_enabled,
                "routines_enabled": settings.routines_enabled,
                "stopped": bool(gate(session).stopped),
                "worker_slots": settings.worker_slots,
                "unconfirmed_local_runs": local,
                "unconfirmed_remote_jobs": remote,
                "legacy_untracked_runs": legacy,
                "resource_release": "pending" if local or remote else "unknown" if legacy else "confirmed",
            }

    @router.get("/api/agents/{ident}")
    def get_agent(ident: str):
        with db.sessions() as session:
            agent = require(session, Agent, ident)
            return {**as_dict(agent), "definition": definition(agent).model_dump(mode="json")}

    @router.post("/api/agents", status_code=201)
    def create_agent(request: AgentCreate):
        with db.sessions.begin() as session:
            check_admission(session, settings)
            if session.get(Agent, request.id):
                raise Conflict("Bot id already exists")
            validate_definition(session, request.definition, settings)
            request.definition.definition_version = 1
            agent = Agent(**request.model_dump(mode="json"))
            if request.definition.default_computer_id:
                from niucai.storage.db import Computer

                require(session, Computer, request.definition.default_computer_id)
            session.add(agent)
            session.flush()
            emit(session, "agent.updated", agent_id=agent.id)
            return as_dict(agent)

    @router.patch("/api/agents/{ident}")
    def patch_agent(ident: str, request: AgentPatch):
        with db.sessions.begin() as session:
            gate(session)
            agent = require(session, Agent, ident, lock=True)
            if request.definition:
                validate_definition(session, request.definition, settings)
                request.definition.definition_version = definition(agent).definition_version + 1
            if request.definition and request.definition.archived:
                archive(session, ident)
            for key, value in request.model_dump(mode="json", exclude_unset=True).items():
                if value is None:
                    raise HTTPException(422, "name and definition cannot be null")
                setattr(agent, key, value)
            emit(session, "agent.updated", agent_id=agent.id)
            return as_dict(agent)

    @router.delete("/api/agents/{ident}")
    def delete_agent(ident: str):
        with db.sessions.begin() as session:
            gate(session)
            agent = archive(session, ident)
            emit(session, "agent.updated", agent_id=agent.id)
            return as_dict(agent)

    @router.post("/api/agents/stop-all")
    def stop_all():
        with db.sessions.begin() as session:
            gate(session).stopped = 1
            for workflow in session.scalars(
                select(Workflow).where(Workflow.status.in_(["ACTIVE", "PAUSED"]))
            ):
                workflow.status = "CANCELLED"
                cancel_items(
                    session,
                    workflow,
                    list(session.scalars(select(WorkItem).where(WorkItem.workflow_id == workflow.id))),
                )
            from niucai.control.remote import cancel_children

            for task in session.scalars(select(Task).where(Task.status.not_in(FINISHED)).order_by(Task.id)):
                settle_task(session, task, "CANCELLED")
                cancel_children(session, task.id)
            for job in session.scalars(
                select(RemoteJob).where(
                    RemoteJob.status.not_in(["COMPLETED", "FAILED", "CANCELLED", "DENIED"])
                )
            ):
                job.status = "CANCELLING" if job.status in {"RUNNING", "LOST", "CANCELLING"} else "CANCELLED"
            emit(session, "agents.stopped")
        # Process shutdown is asynchronous. Report pending confirmation truthfully.
        return status()

    @router.post("/api/agents/resume-all")
    def resume_all():
        with db.sessions.begin() as session:
            gate(session).stopped = 0
            for routine in session.scalars(select(Routine).where(Routine.enabled == 1)):
                from niucai.control.routines import next_time

                routine.next_run_at = next_time(routine.schedule, routine.timezone)
            emit(session, "agents.resumed")
        return {"stopped": False}

    @router.get("/api/workflows")
    def workflows(limit: int = Query(100, ge=1, le=200)):
        with db.sessions() as session:
            return [
                as_dict(w)
                for w in session.scalars(select(Workflow).order_by(Workflow.created_at.desc()).limit(limit))
            ]

    @router.post("/api/workflows", status_code=201)
    def create_workflow(request: WorkflowCreate):
        from niucai.control.collaboration import CollaborationManager
        from niucai.control.conversations import ConversationManager, append_message

        with db.sessions.begin() as session:
            check_admission(session, settings)
            active_agent(session, request.agent_id)
            for ident in (
                {a for route in request.routes for a in route}
                | set(request.final_agent_ids)
                | set(request.participant_agent_ids or [])
            ):
                active_agent(session, ident)
            if request.stages is not None:
                return create_pipeline(session, request)
            if request.routes and not request.final_agent_ids:
                raise HTTPException(422, "stage workflow requires final_agent_ids")
            execution = ConversationManager.create_in(session, request.goal[:80], request.agent_id)
            inherit(session, request.origin_conversation_id, execution)
            task = Task(
                title=request.goal[:80],
                goal=request.goal,
                agent_id=request.agent_id,
                conversation_id=execution.id,
                checkpoint={"runtime": "pi", "session_scope": "conversation"},
            )
            session.add(task)
            session.flush()
            workflow, item = CollaborationManager(db, settings).root(session, task, dedicated=True)
            item.output_requirements = request.output_requirements
            if request.participant_agent_ids is not None:
                workflow.authorization = {
                    **workflow.authorization,
                    "agents": sorted(set([request.agent_id, *request.participant_agent_ids])),
                }
            workflow.routes = [list(route) for route in request.routes]
            workflow.limits = {
                **workflow.limits,
                "revisions": request.max_revisions,
                "final_agent_ids": request.final_agent_ids,
            }
            if request.origin_conversation_id:
                require(session, Conversation, request.origin_conversation_id)
                workflow.origin_conversation_id = request.origin_conversation_id
                workflow.authorization = {**workflow.authorization, "project_results": True}
            append_message(
                session,
                execution,
                role="user",
                content=request.goal,
                task_id=task.id,
                workflow_id=workflow.id,
                work_item_id=item.id,
            )
            return {**as_dict(workflow), "work_items": [item_dict(session, item)]}

    def create_pipeline(session, request):
        from niucai.control.collaboration import create_stages
        from niucai.control.conversations import ConversationManager, append_message

        if request.routes or request.final_agent_ids:
            raise HTTPException(422, "pipeline stages cannot be combined with routes")
        pipeline_order(request.stages)
        if request.origin_conversation_id:
            origin = require(session, Conversation, request.origin_conversation_id)
        else:
            # 概览会话只汇总各阶段结论，本身不启动执行。
            origin = ConversationManager.create_in(session, request.goal[:80], request.agent_id)
            append_message(session, origin, role="user", content=request.goal)
        authorization = {
            "tools": definition(require(session, Agent, request.agent_id)).tools,
            "project_results": True,
        }
        if request.participant_agent_ids is not None:
            authorization["agents"] = sorted({request.agent_id, *request.participant_agent_ids})
        workflow = Workflow(
            origin_conversation_id=origin.id,
            owner_agent_id=request.agent_id,
            authorization=authorization,
            limits={
                "handoffs": settings.handoff_max_per_workflow,
                "model_turns": settings.workflow_max_model_turns,
                "seconds": settings.workflow_max_seconds,
                "revisions": request.max_revisions,
            },
        )
        session.add(workflow)
        session.flush()
        emit(session, "workflow.created", workflow_id=workflow.id)
        create_stages(session, settings, workflow, request.stages)
        return {
            **as_dict(workflow),
            "work_items": [
                item_dict(session, i)
                for i in session.scalars(select(WorkItem).where(WorkItem.workflow_id == workflow.id))
            ],
        }

    @router.get("/api/workflows/{ident}")
    def workflow(ident: str):
        with db.sessions() as session:
            return {
                **as_dict(require(session, Workflow, ident)),
                "work_items": [
                    item_dict(session, i)
                    for i in session.scalars(select(WorkItem).where(WorkItem.workflow_id == ident))
                ],
            }

    @router.post("/api/workflows/{ident}/{operation}")
    def control_workflow(ident: str, operation: Literal["pause", "resume", "cancel"]):
        with db.sessions.begin() as session:
            control = gate(session)
            workflow = require(session, Workflow, ident, lock=True)
            if workflow.status not in {"ACTIVE", "PAUSED"}:
                raise Conflict("workflow already finished")
            items = list(session.scalars(select(WorkItem).where(WorkItem.workflow_id == ident)))
            if operation == "cancel":
                workflow.status = "CANCELLED"
                cancel_items(session, workflow, items)
            elif operation == "pause":
                workflow.status = "PAUSED"
                for item in sorted(items, key=lock_order):
                    if item.status not in FINISHED:
                        task = require(session, Task, item.task_id, lock=True)
                        settle_task(session, task, "PAUSED")
                        item.status = "PAUSED"
            else:
                if control.stopped or not settings.multi_bot_enabled:
                    raise Conflict("Bot admission is disabled")
                workflow.status = "ACTIVE"
                for item in items:
                    if item.status == "PAUSED":
                        task = require(session, Task, item.task_id, lock=True)
                        task.status = "WAITING_BOT" if item.wait else "PENDING"
                        unresolved = session.scalar(
                            select(Action)
                            .where(
                                Action.task_id == task.id, Action.status.in_(["WAITING_APPROVAL", "UNKNOWN"])
                            )
                            .limit(1)
                        )
                        if unresolved and not item.wait:
                            task.status = "WAITING_HUMAN"
                            task.checkpoint = {
                                **task.checkpoint,
                                "reason": unresolved.status,
                                "action_id": unresolved.id,
                            }
                        item.status = "WAITING_BOT" if item.wait else "QUEUED"
                        sync_human_wait(session, task)
                        # Approval/UNKNOWN is still enforced by the action gateway and wait guard.
                        wake(session, item)
            workflow.version += 1
            emit(session, f"workflow.{operation}", workflow_id=ident)
            return as_dict(workflow)

    @router.get("/api/work-items")
    def items(workflow_id: str | None = None, limit: int = Query(100, ge=1, le=200)):
        with db.sessions() as session:
            query = select(WorkItem)
            if workflow_id:
                query = query.where(WorkItem.workflow_id == workflow_id)
            return [item_dict(session, i) for i in session.scalars(query.limit(limit))]

    @router.get("/api/work-items/{ident}")
    def item(ident: str):
        with db.sessions() as session:
            return item_dict(session, require(session, WorkItem, ident))

    @router.patch("/api/work-items/{ident}")
    def update_item(ident: str, request: BoardPatch):
        with db.sessions.begin() as session:
            gate(session)
            item = require(session, WorkItem, ident, lock=True)
            if item.version != request.expected_version:
                raise Conflict("board version conflict")
            before = {"labels": item.labels, "due_at": str(item.due_at)}
            item.labels, item.due_at = request.labels, request.due_at
            item.version += 1
            emit(
                session,
                "work_item.updated",
                work_item_id=ident,
                actor="human",
                before=before,
                after={"labels": item.labels, "due_at": str(item.due_at)},
                version=item.version,
            )
            return item_dict(session, item)

    @router.get("/api/handoffs")
    def handoffs(workflow_id: str | None = None, conversation_id: str | None = None):
        with db.sessions() as session:
            query = select(Handoff)
            if workflow_id:
                query = query.where(Handoff.workflow_id == workflow_id)
            if conversation_id:
                query = query.where(
                    (Handoff.from_conversation_id == conversation_id)
                    | (Handoff.to_conversation_id == conversation_id)
                )
            return [as_dict(h) for h in session.scalars(query.order_by(Handoff.created_at).limit(200))]

    @router.get("/api/routines")
    def routines():
        with db.sessions() as session:
            return [as_dict(r) for r in session.scalars(select(Routine).limit(200))]

    @router.post("/api/routines", status_code=201)
    def create_routine(request: RoutineCreate):
        from niucai.control.routines import next_time

        try:
            upcoming = next_time(request.schedule, request.timezone)
        except (ValueError, KeyError) as exc:
            raise HTTPException(422, str(exc)) from exc
        with db.sessions.begin() as session:
            check_admission(session, settings)
            active_agent(session, request.agent_id)
            if request.conversation_id:
                require(session, Conversation, request.conversation_id)
            routine = Routine(**request.model_dump(), next_run_at=upcoming)
            session.add(routine)
            session.flush()
            return as_dict(routine)

    @router.patch("/api/routines/{ident}")
    def update_routine(ident: str, request: RoutinePatch):
        from niucai.control.routines import next_time

        with db.sessions.begin() as session:
            gate(session)
            routine = require(session, Routine, ident, lock=True)
            merged = {name: getattr(routine, name) for name in RoutineCreate.model_fields}
            merged.update(request.model_dump(exclude_unset=True))
            try:
                validated = RoutineCreate.model_validate(merged)
                upcoming = next_time(validated.schedule, validated.timezone)
            except (ValueError, KeyError, ValidationError) as exc:
                raise HTTPException(422, str(exc)) from exc
            active_agent(session, validated.agent_id)
            if validated.conversation_id:
                require(session, Conversation, validated.conversation_id)
            for name, value in validated.model_dump().items():
                setattr(routine, name, value)
            routine.next_run_at = upcoming
            return as_dict(routine)

    @router.delete("/api/routines/{ident}")
    def disable_routine(ident: str):
        with db.sessions.begin() as session:
            gate(session)
            routine = require(session, Routine, ident, lock=True)
            routine.enabled = 0
            return as_dict(routine)

    @router.get("/api/routines/{ident}/runs")
    def routine_runs(ident: str):
        with db.sessions() as session:
            require(session, Routine, ident)
            return [
                as_dict(r)
                for r in session.scalars(
                    select(RoutineRun)
                    .where(RoutineRun.routine_id == ident)
                    .order_by(RoutineRun.scheduled_at.desc())
                    .limit(100)
                )
            ]

    @router.get("/api/conversations/{ident}/participants")
    def participants(ident: str):
        with db.sessions() as session:
            require(session, Conversation, ident)
            return [
                as_dict(p)
                for p in session.scalars(
                    select(ConversationParticipant).where(ConversationParticipant.conversation_id == ident)
                )
            ]

    @router.put("/api/conversations/{ident}/participants")
    def update_participants(ident: str, request: Participants):
        if (
            len(set(request.agent_ids)) != len(request.agent_ids)
            or request.coordinator not in request.agent_ids
        ):
            raise HTTPException(422, "distinct participants and one coordinator required")
        with db.sessions.begin() as session:
            check_admission(session, settings)
            conversation = require(session, Conversation, ident, lock=True)
            if session.scalar(select(Task.id).where(Task.conversation_id == ident)):
                raise Conflict("create a fresh display conversation before configuring a group")
            for agent_id in request.agent_ids:
                active_agent(session, agent_id)
            for participant in session.scalars(
                select(ConversationParticipant).where(ConversationParticipant.conversation_id == ident)
            ):
                session.delete(participant)
            session.flush()
            for agent_id in request.agent_ids:
                session.add(
                    ConversationParticipant(
                        conversation_id=ident,
                        agent_id=agent_id,
                        role="coordinator" if agent_id == request.coordinator else "member",
                    )
                )
            conversation.agent_id = request.coordinator
            return request.model_dump()

    return router
