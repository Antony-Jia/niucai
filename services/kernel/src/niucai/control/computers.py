from sqlalchemy import select

from niucai.control.tasks import Conflict, require
from niucai.storage.db import Computer, Task, TaskRun, emit, now


class ComputerManager:
    def __init__(self, db):
        self.db = db

    def control(self, computer_id, operation):
        # Lock tasks before computer everywhere to avoid opposite-order deadlocks.
        with self.db.sessions.begin() as s:
            tasks = s.scalars(
                select(Task)
                .where(Task.computer_id == computer_id)
                .where(Task.status.in_(["PENDING", "RUNNING", "PAUSED", "WAITING_HUMAN"]))
                .order_by(Task.id)
                .with_for_update()
            ).all()
            computer = require(s, Computer, computer_id, lock=True)
            target = {"take-control": "HUMAN", "hand-back": "AGENT", "pause": "PAUSED", "resume": "AGENT"}[
                operation
            ]
            if operation == "resume" and computer.control == "HUMAN":
                raise Conflict("use hand-back to release human control")
            computer.control = target
            computer.state = {**computer.state, "refs": []}
            if target != "AGENT":
                for task in tasks:
                    if task.status not in {"PENDING", "RUNNING"}:
                        continue
                    if task.run_token:
                        run = s.get(TaskRun, task.run_token)
                        if run:
                            run.status, run.ended_at = "PAUSED", now()
                    task.checkpoint = {**task.checkpoint, "paused_by_computer": True}
                    task.status, task.run_token, task.lease_until = "PAUSED", None, None
                    emit(s, "task.paused", task.id, reason=operation)
            else:
                paused = s.scalars(
                    select(Task).where(Task.computer_id == computer_id, Task.status == "PAUSED")
                )
                for task in paused:
                    if task.checkpoint.get("paused_by_computer"):
                        task.checkpoint = {**task.checkpoint, "paused_by_computer": False}
                        task.status = "PENDING"
                        emit(s, "task.resumed", task.id)
            emit(s, "computer.control_changed", computer_id=computer.id, control=target)
            for task in tasks:
                emit(s, "task.computer_control_changed", task.id, control=target)
            return computer
