from sqlalchemy import select

from niucai.actions.gateway import ActionGateway
from niucai.control.bots import memory_access, memory_query, permitted
from niucai.control.collaboration import gate
from niucai.control.receipts import receipt, save_receipt
from niucai.control.tasks import Conflict, require
from niucai.storage.db import Agent, Memory, Task, as_dict, emit


def memory_tool(db, task_id, token, method, params):
    operation = method.split(".")[1]
    with db.sessions.begin() as session:
        if gate(session).stopped:
            raise Conflict("all Bots are stopped")
        task = require(session, Task, task_id, lock=True)
        ActionGateway.check_task(task, token)
        permitted(session, task, f"memory.{operation}")
        key, result = receipt(session, task, method, params)
        if result is not None:
            memory_access(session, task, operation, Memory(agent_id=result["agent_id"]))
            return result
        if operation == "read":
            memory_access(session, task, operation)
            return [
                as_dict(m)
                for m in session.scalars(
                    select(Memory)
                    .where(memory_query(require(session, Agent, task.agent_id)))
                    .order_by(Memory.created_at.desc())
                    .limit(100)
                )
            ]
        memory = require(session, Memory, params["id"], lock=True) if params.get("id") else None
        owner = memory_access(session, task, operation, memory, params.get("shared", False))
        if operation == "create":
            memory = Memory(
                agent_id=owner,
                task_id=task.id,
                kind=params.get("kind", "semantic"),
                content=params["content"],
                tags=params.get("tags", []),
            )
            session.add(memory)
            session.flush()
        elif operation == "update" and memory:
            memory.content = params["content"]
        elif operation == "delete" and memory:
            session.delete(memory)
        else:
            raise ValueError("invalid memory operation")
        if operation != "delete" and (not memory.content.strip() or len(memory.content) > 16000):
            raise ValueError("invalid memory content")
        emit(session, f"memory.{operation}", task.id, memory_id=memory.id, actor_agent_id=task.agent_id)
        result = as_dict(memory)
        save_receipt(session, key, task, params, result)
        return result
