from datetime import timedelta

from sqlalchemy import or_, select

from niucai.domain.schemas import TaskCreate
from niucai.storage.db import Agent, Computer, Task, TaskRun, emit, now, uid


class Conflict(Exception):
    pass


class Missing(Exception):
    pass


class ComputerRequired(ValueError):
    """A recoverable configuration problem, not a revoked worker lease."""

    pass


def require(session, model, ident, lock=False):
    query = select(model).where(model.id == ident)
    if lock:
        query = query.with_for_update()
    row = session.scalar(query)
    if row is None:
        raise Missing(f"{model.__name__} not found")
    return row


class TaskManager:
    def __init__(self, db, settings):
        self.db, self.settings = db, settings

    def create(self, request: TaskCreate):
        with self.db.sessions.begin() as s:
            require(s, Agent, request.agent_id)
            if request.computer_id:
                require(s, Computer, request.computer_id)
            task = Task(**request.model_dump())
            s.add(task)
            s.flush()
            emit(s, "task.created", task.id, title=task.title)
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
                if task.status == "RUNNING":
                    old = s.get(TaskRun, task.run_token)
                    if old:
                        old.status, old.ended_at = "EXPIRED", now()
                    task.retry_count += 1
                    emit(s, "task.recovered", task.id)
                task.status, task.run_token = "RUNNING", uid()
                task.lease_until = now() + timedelta(seconds=self.settings.lease_seconds)
                task.heartbeat_at = now()
                s.add(TaskRun(id=task.run_token, task_id=task.id))
                emit(s, "task.started", task.id, run_token=task.run_token)
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
            for field, value in changes.items():
                setattr(task, field, value)
            if task.status != "RUNNING":
                run = require(s, TaskRun, token)
                run.status, run.ended_at = task.status, now()
                task.run_token, task.lease_until = None, None
                emit(s, f"task.{task.status.lower()}", task.id)
            return task
