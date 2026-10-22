from datetime import UTC
from zoneinfo import ZoneInfo

from croniter import croniter
from sqlalchemy import select

from niucai.control.bots import active_agent, definition
from niucai.control.collaboration import CollaborationManager, check_admission, gate
from niucai.control.conversations import ConversationManager, append_message
from niucai.storage.db import Agent, Routine, RoutineRun, Task, Workflow, emit, now


def next_time(schedule, timezone, base=None):
    if len(schedule.split()) != 5:
        raise ValueError("Routine schedule must use five-field minute cron syntax")
    zone = ZoneInfo(timezone)
    local = (base or now()).replace(tzinfo=UTC).astimezone(zone)
    return croniter(schedule, local).get_next(type(local)).astimezone(UTC).replace(tzinfo=None)


def fire_due(db, settings):
    if not (settings.multi_bot_enabled and settings.routines_enabled):
        return
    with db.sessions.begin() as session:
        if gate(session).stopped:
            return
        check_admission(session, settings)
        for routine in session.scalars(
            select(Routine)
            .where(Routine.enabled == 1, Routine.next_run_at <= now())
            .with_for_update(skip_locked=True)
        ):
            # After downtime admit only the most recent scheduled occurrence.
            local = now().replace(tzinfo=UTC).astimezone(ZoneInfo(routine.timezone))
            scheduled = (
                croniter(routine.schedule, local).get_prev(type(local)).astimezone(UTC).replace(tzinfo=None)
            )
            scheduled = max(scheduled, routine.next_run_at)
            exists = session.scalar(
                select(RoutineRun.id).where(
                    RoutineRun.routine_id == routine.id, RoutineRun.scheduled_at == scheduled
                )
            )
            routine.next_run_at = next_time(routine.schedule, routine.timezone)
            if exists:
                continue
            agent = session.get(Agent, routine.agent_id)
            if not agent or not definition(agent).enabled or definition(agent).archived:
                session.add(RoutineRun(routine_id=routine.id, scheduled_at=scheduled, status="SKIPPED"))
                emit(session, "routine.skipped", routine_id=routine.id, reason="Bot disabled")
                continue
            active = session.scalar(
                select(RoutineRun.id)
                .join(Workflow, Workflow.id == RoutineRun.workflow_id)
                .where(RoutineRun.routine_id == routine.id, Workflow.status.in_(["ACTIVE", "PAUSED"]))
                .limit(1)
            )
            if active:
                session.add(RoutineRun(routine_id=routine.id, scheduled_at=scheduled, status="SKIPPED"))
                emit(session, "routine.skipped", routine_id=routine.id, reason="previous run still active")
                continue
            active_agent(session, routine.agent_id)
            conversation = ConversationManager.create_in(session, routine.prompt[:80], routine.agent_id)
            task = Task(
                title=routine.prompt[:80],
                goal=routine.prompt,
                conversation_id=conversation.id,
                agent_id=routine.agent_id,
                checkpoint={"runtime": "pi", "session_scope": "conversation"},
            )
            session.add(task)
            session.flush()
            workflow, item = CollaborationManager(db, settings).root(session, task, dedicated=True)
            if routine.conversation_id:
                workflow.origin_conversation_id = routine.conversation_id
                workflow.authorization = {**workflow.authorization, "project_results": True}
            append_message(
                session,
                conversation,
                role="bot",
                source="ROUTINE",
                kind="ASSIGNMENT",
                content=routine.prompt,
                routine_id=routine.id,
                task_id=task.id,
                workflow_id=workflow.id,
                work_item_id=item.id,
            )
            session.add(
                RoutineRun(
                    routine_id=routine.id, scheduled_at=scheduled, workflow_id=workflow.id, status="ADMITTED"
                )
            )
            routine.last_run_at = now()
            emit(
                session,
                "routine.fired",
                routine_id=routine.id,
                workflow_id=workflow.id,
                conversation_id=routine.conversation_id,
            )
