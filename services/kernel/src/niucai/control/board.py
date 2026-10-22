from sqlalchemy import select

from niucai.actions.gateway import ActionGateway
from niucai.control.bots import permitted
from niucai.control.collaboration import gate
from niucai.control.receipts import receipt, save_receipt
from niucai.control.tasks import Conflict, require
from niucai.storage.db import Task, WorkItem, as_dict, emit


def board_tool(db, task_id, token, method, params):
    with db.sessions.begin() as session:
        if gate(session).stopped:
            raise Conflict("all Bots are stopped")
        task = require(session, Task, task_id, lock=True)
        ActionGateway.check_task(task, token)
        permitted(session, task, method.replace(".", "_"))
        key, result = receipt(session, task, method, params)
        if result is not None:
            return result
        own = session.scalar(select(WorkItem).where(WorkItem.task_id == task.id))
        if not own:
            raise PermissionError("board requires an associated workflow")
        if method == "board.list":
            return [
                as_dict(i)
                for i in session.scalars(select(WorkItem).where(WorkItem.workflow_id == own.workflow_id))
            ]
        if method != "board.update":
            raise ValueError("unknown board tool")
        item = require(session, WorkItem, params["work_item_id"], lock=True)
        if item.workflow_id != own.workflow_id:
            raise PermissionError("work item outside workflow")
        if item.version != params["expected_version"]:
            raise Conflict("board version conflict")
        before = item.labels
        item.labels = list(params["labels"])
        item.version += 1
        emit(
            session,
            "work_item.updated",
            task.id,
            work_item_id=item.id,
            actor_agent_id=task.agent_id,
            before={"labels": before},
            after={"labels": item.labels},
            version=item.version,
        )
        result = as_dict(item)
        save_receipt(session, key, task, params, result)
        return result
