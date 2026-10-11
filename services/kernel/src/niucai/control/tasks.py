from datetime import timedelta

from sqlalchemy import or_, select

from niucai.control.progress import TERMINAL, clear_wait, task_progress
from niucai.domain.schemas import TaskCreate
from niucai.storage.db import Action, Agent, Computer, Conversation, Message, Task, TaskRun, emit, now, uid


class Conflict(Exception):
    pass


class Missing(Exception):
    pass


class ComputerRequired(ValueError):
    """A recoverable configuration problem, not a revoked worker lease."""

    pass


def require(session, model, ident, lock=False):
    if lock and model is Task:
        row = session.get(Task, ident)
        if row and row.conversation_id:
            from niucai.control.conversations import lock_conversation

            lock_conversation(session, row.conversation_id)
    query = select(model).where(model.id == ident).execution_options(populate_existing=True)
    if lock:
        query = query.with_for_update()
    row = session.scalar(query)
    if row is None:
        raise Missing(f"{model.__name__} not found")
    return row


class TaskManager:
    def __init__(self, db, settings):
        self.db, self.settings = db, settings

    def create(self, request: TaskCreate, *, unified=False):
        with self.db.sessions.begin() as s:
            require(s, Agent, request.agent_id)
            if request.computer_id:
                require(s, Computer, request.computer_id)
            from niucai.control.conversations import ConversationManager, append_message

            conversation = ConversationManager.create_in(
                s, request.title, request.agent_id, request.computer_id
            )
            task = Task(
                **request.model_dump(),
                conversation_id=conversation.id,
                checkpoint={"session_scope": "conversation", "runtime": "pi"} if unified else {},
            )
            s.add(task)
            s.flush()
            emit(s, "task.created", task.id, title=task.title)
            append_message(s, conversation, role="user", content=task.goal, task_id=task.id)
            return task

    def transition(self, task_id, operation):
        with self.db.sessions.begin() as s:
            task = require(s, Task, task_id, lock=True)
            allowed = {
                "pause": ({"PENDING", "RUNNING", "WAITING_HUMAN"}, "PAUSED"),
                "resume": ({"PAUSED", "WAITING_HUMAN"}, "PENDING"),
                "retry": ({"FAILED"}, "PENDING"),
                "cancel": ({"PENDING", "RUNNING", "PAUSED", "WAITING_HUMAN", "FAILED"}, "CANCELLED"),
            }
            sources, destination = allowed[operation]
            if task.status not in sources:
                raise Conflict(f"cannot {operation} task in {task.status}")
            if operation not in task_progress(s, task)["allowed_operations"]:
                raise Conflict("resolve approvals, uncertain actions or computer control before continuing")
            if task.run_token:
                run = s.get(TaskRun, task.run_token)
                if run:
                    run.status, run.ended_at = destination, now()
            task.status, task.run_token, task.lease_until = destination, None, None
            if operation in {"retry", "resume"} and task.checkpoint.get("reason") == "step budget exhausted":
                task.checkpoint = {
                    **task.checkpoint,
                    "budget_limit": task.current_step + self.settings.max_steps,
                }
            if operation == "resume" and task.checkpoint.get("reason") == "agent requested human":
                task.checkpoint = {
                    **task.checkpoint,
                    "pi_human_resumed": task.checkpoint.get("pi_human_wait"),
                }
            if operation == "retry":
                task.retry_count += 1
            if operation in {"retry", "resume", "cancel"}:
                task.checkpoint = clear_wait(task.checkpoint)
            if operation == "retry":
                previous = {k: task.checkpoint[k] for k in ("error", "error_detail") if k in task.checkpoint}
                task.checkpoint = {
                    k: v for k, v in task.checkpoint.items() if k not in {"error", "error_detail"}
                }
                if previous:
                    task.checkpoint = {**task.checkpoint, "last_error": previous}
            if operation == "retry":
                pending = s.scalar(
                    select(Action)
                    .where(Action.task_id == task.id, Action.status == "WAITING_APPROVAL")
                    .order_by(Action.created_at, Action.id)
                    .limit(1)
                )
                if pending:
                    destination = task.status = "WAITING_HUMAN"
                    task.checkpoint = {
                        **task.checkpoint,
                        "reason": "WAITING_APPROVAL",
                        "action_id": pending.id,
                    }
            if operation == "pause":
                task.checkpoint = {**task.checkpoint, "paused_by_computer": False}
            if operation in {"pause", "cancel"}:
                from niucai.control.remote import cancel_children

                cancel_children(s, task.id)
            emit(s, f"task.{operation}", task.id, status=destination)
            return task

    def attach_computer(self, task_id, computer_id):
        with self.db.sessions.begin() as s:
            task = require(s, Task, task_id, lock=True)
            if task.status not in {"PENDING", "PAUSED", "WAITING_HUMAN"}:
                raise Conflict("pause the task before choosing a computer")
            if task.computer_id and task.computer_id != computer_id:
                raise Conflict("task already belongs to another computer")
            computer = require(s, Computer, computer_id, lock=True)
            if computer.kind != "linux":
                raise Conflict("browser tasks require a Linux computer")
            task.computer_id = computer_id
            if task.conversation_id:
                require(s, Conversation, task.conversation_id).computer_id = computer_id
            if task.checkpoint.get("reason") == "computer required":
                task.checkpoint = {k: v for k, v in task.checkpoint.items() if k not in {"reason", "detail"}}
            emit(s, "task.computer_assigned", task.id, computer_id=computer_id)
            return task

    def claim(self):
        with self.db.sessions.begin() as s:
            tasks = s.scalars(
                select(Task)
                .where(or_(Task.status == "PENDING", (Task.status == "RUNNING") & (Task.lease_until < now())))
                .order_by(Task.created_at)
                .with_for_update(skip_locked=True)
                .limit(32)
            ).all()
            for task in tasks:
                computer = require(s, Computer, task.computer_id, lock=True) if task.computer_id else None
                if computer:
                    if computer.control != "AGENT":
                        continue
                    if computer.active_task_id and computer.active_task_id != task.id:
                        other = s.get(Task, computer.active_task_id)
                        if (
                            other
                            and other.status == "RUNNING"
                            and other.lease_until
                            and other.lease_until > now()
                        ):
                            continue
                    computer.active_task_id = task.id
                recovered = task.status == "RUNNING"
                if recovered:
                    old = s.get(TaskRun, task.run_token)
                    if old:
                        old.status, old.ended_at = "EXPIRED", now()
                    task.retry_count += 1
                    emit(s, "task.recovered", task.id)
                task.status, task.run_token = "RUNNING", uid()
                task.lease_until = now() + timedelta(seconds=self.settings.lease_seconds)
                task.heartbeat_at = now()
                s.add(TaskRun(id=task.run_token, task_id=task.id))
                emit(s, "task.started", task.id, run_token=task.run_token, recovered=recovered)
                return task
            return None

    def heartbeat(self, task_id, token):
        with self.db.sessions.begin() as s:
            task = require(s, Task, task_id, lock=True)
            if task.status != "RUNNING" or task.run_token != token or task.lease_until < now():
                return False
            task.heartbeat_at = now()
            task.lease_until = now() + timedelta(seconds=self.settings.lease_seconds)
            return True

    def update(self, task_id, token, **changes):
        with self.db.sessions.begin() as s:
            task = require(s, Task, task_id, lock=True)
            if task.status != "RUNNING" or task.run_token != token or task.lease_until < now():
                raise Conflict("worker lost its task lease")
            # Callers may carry an older checkpoint across a model/tool await.
            # Preserve the clock/phase journal written by intervening business events.
            if "checkpoint" in changes:
                journal = task.checkpoint.get("_progress")
                if journal:
                    changes["checkpoint"] = {**changes["checkpoint"], "_progress": journal}
            if (
                changes.get("status") == "COMPLETED"
                and task.checkpoint.get("session_scope") == "conversation"
            ):
                pending = s.scalar(
                    select(Message.id)
                    .where(Message.task_id == task.id, Message.role == "user", Message.delivered_at.is_(None))
                    .limit(1)
                )
                if pending:
                    # A message racing the final answer must get another execution opportunity.
                    changes["status"] = "PENDING"
                    changes["checkpoint"] = {
                        **changes.get("checkpoint", task.checkpoint),
                        "pi_continue_inbox": True,
                    }
            if changes.get("status") in {"COMPLETED", "PENDING"} and "result" in changes.get(
                "checkpoint", {}
            ):
                if task.conversation_id:
                    from niucai.control.conversations import append_message

                    conversation = require(s, Conversation, task.conversation_id)
                    append_message(
                        s,
                        conversation,
                        role="assistant",
                        task_id=task.id,
                        content=str(changes["checkpoint"]["result"]),
                    )
            for field, value in changes.items():
                setattr(task, field, value)
            # Approval may be decided after execute() returns WAITING_APPROVAL
            # but before the harness checkpoints its wait. Do not strand it.
            if task.status == "WAITING_HUMAN" and task.checkpoint.get("reason") == "WAITING_APPROVAL":
                action = (
                    s.get(Action, task.checkpoint.get("action_id"))
                    if task.checkpoint.get("action_id")
                    else None
                )
                unresolved = s.scalar(
                    select(Action.id)
                    .where(Action.task_id == task.id, Action.status.in_(["WAITING_APPROVAL", "UNKNOWN"]))
                    .limit(1)
                )
                if (
                    action
                    and action.status in {"APPROVED", "DENIED", "SUCCEEDED", "FAILED"}
                    and not unresolved
                ):
                    task.status = "PENDING"
                    task.checkpoint = clear_wait(task.checkpoint)
            if task.status in TERMINAL:
                task.checkpoint = clear_wait(task.checkpoint, failed=task.status == "FAILED")
            if task.status != "RUNNING":
                run = require(s, TaskRun, token)
                run.status, run.ended_at = task.status, now()
                task.run_token, task.lease_until = None, None
                emit(s, f"task.{task.status.lower()}", task.id)
            elif "plan" in changes:
                emit(s, "task.plan_updated", task.id)
            elif "current_step" in changes:
                emit(s, "task.step_completed", task.id, current_step=task.current_step)
            return task

    def phase(self, task_id, token, phase):
        if phase not in {"PLANNING", "MODEL_REQUEST", "PROCESSING", "WAITING_REMOTE"}:
            raise ValueError("unsupported worker phase")
        with self.db.sessions.begin() as s:
            task = require(s, Task, task_id, lock=True)
            if task.status != "RUNNING" or task.run_token != token or task.lease_until < now():
                raise Conflict("worker lost its task lease")
            emit(s, "task.phase_changed", task.id, phase=phase)
