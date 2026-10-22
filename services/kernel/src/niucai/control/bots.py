"""Agent definitions and enforced tool/namespace policy, independent of prompts."""

from fnmatch import fnmatchcase

from sqlalchemy import or_, select

from niucai.control.tasks import Conflict, require
from niucai.domain.schemas import AgentDefinition
from niucai.storage.db import Agent, Memory, Routine, Task, WorkItem


def definition(agent):
    # Explicit compatibility for pre-migration main only.
    legacy = {"tools": ["*"], "allowed_nodes": None} if agent.id == "main" else {}
    return AgentDefinition.model_validate({**legacy, **agent.definition})


def allows(patterns, tool):
    return any(fnmatchcase(tool, pattern) for pattern in patterns)


def task_definition(session, task):
    item = session.scalar(select(WorkItem).where(WorkItem.task_id == task.id))
    if item and item.definition_snapshot:
        return AgentDefinition.model_validate(item.definition_snapshot)
    return definition(require(session, Agent, task.agent_id))


def validate_definition(session, value, settings):
    from niucai.models.gateway import ModelRouter
    from niucai.storage.db import Computer, RemoteNode

    if value.default_computer_id:
        require(session, Computer, value.default_computer_id)
    for ident in value.allowed_nodes or []:
        require(session, RemoteNode, ident)
    if value.resource_policy.fixed_node_id:
        require(session, RemoteNode, value.resource_policy.fixed_node_id)
    for ident in value.delegates or []:
        require(session, Agent, ident)
    if value.model_roles:
        router = ModelRouter(settings.model_config_path)
        for role in value.model_roles.values():
            router.resolve(role)


# 只读的 Kernel 技能说明对所有 Bot 默认开放，仅受 definition.skills 白名单收窄。
IMPLICIT_TOOLS = {"skills.read"}


def permitted(session, task, tool):
    agent = definition(require(session, Agent, task.agent_id))
    if tool in IMPLICIT_TOOLS and agent.enabled and not agent.archived:
        return task_definition(session, task)
    if not agent.enabled or agent.archived or not allows(agent.tools, tool):
        raise PermissionError(f"Bot {task.agent_id} cannot use {tool}")
    saved = task_definition(session, task)
    if saved.resource_policy.strategy == "fixed" and tool in {"shell.exec", "files.read", "files.write"}:
        raise PermissionError(
            "fixed resource Bot must use an approved remote work package for workspace actions"
        )
    if not allows(saved.tools, tool):
        raise PermissionError(f"Bot configuration snapshot denies {tool}")
    scopes = task.checkpoint.get("authorized_tools")
    if scopes is not None and not allows(scopes, tool):
        raise PermissionError(f"workflow authorization denies {tool}")
    if any(not allows(scope, tool) for scope in task.checkpoint.get("authorization_chain", [])):
        raise PermissionError(f"delegating Bot authorization denies {tool}")
    return saved


def memory_query(agent):
    value = definition(agent)
    if value.memory_scope == "own":
        return Memory.agent_id == agent.id
    if value.memory_scope == "shared":
        return Memory.agent_id.is_(None)
    return or_(Memory.agent_id == agent.id, Memory.agent_id.is_(None))


def memory_access(session, task, operation, memory=None, shared=False):
    agent = permitted(session, task, f"memory.{operation}")
    current = definition(require(session, Agent, task.agent_id))
    owner = memory.agent_id if memory else (None if shared else task.agent_id)
    if owner is not None and (
        owner != task.agent_id or any(p.memory_scope == "shared" for p in (agent, current))
    ):
        raise PermissionError("memory belongs to another Bot")
    if owner is None:
        if any(p.memory_scope == "own" for p in (agent, current)):
            raise PermissionError("shared memory outside scope")
        if operation != "read":
            permitted(session, task, "memory.shared_write")
    return owner


def active_agent(session, ident):
    agent = require(session, Agent, ident)
    value = definition(agent)
    if value.archived or not value.enabled:
        raise Conflict("Bot is disabled or archived")
    return agent


def archive(session, ident):
    agent = require(session, Agent, ident, lock=True)
    if ident == "main":
        raise Conflict("main cannot be archived")
    checks = [
        select(Task.id).where(
            Task.agent_id == ident, Task.status.not_in(["COMPLETED", "FAILED", "CANCELLED"])
        ),
        select(WorkItem.id).where(
            WorkItem.assignee_agent_id == ident, WorkItem.status.not_in(["COMPLETED", "FAILED", "CANCELLED"])
        ),
        select(Routine.id).where(Routine.agent_id == ident, Routine.enabled == 1),
    ]
    if any(session.scalar(q.limit(1)) for q in checks):
        raise Conflict("Bot has active tasks, work items or routines")
    value = definition(agent)
    agent.definition = {
        **value.model_dump(mode="json"),
        "archived": True,
        "enabled": False,
        "definition_version": value.definition_version + 1,
    }
    return agent
