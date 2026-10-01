"""Optional DeepAgents harness. Its filesystem is virtual; no direct computer tools.

The gateway-backed model keeps provider selection, metrics and credentials in Kernel.
Kernel owns task state; LangGraph owns the persistent reasoning session.
"""

import json
from typing import Any

from niucai.domain.schemas import Plan, Role


def gateway_model(gateway, role, task_id, guard=None):
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.messages.utils import convert_to_openai_messages
    from langchain_core.outputs import ChatGeneration, ChatResult

    class RoutedChatModel(BaseChatModel):
        bound: dict[str, Any] = {}

        @property
        def _llm_type(self):
            return "niucai-model-gateway"

        def bind_tools(self, tools, **kwargs):
            from langchain_core.utils.function_calling import convert_to_openai_tool

            return self.model_copy(
                update={"bound": {"tools": [convert_to_openai_tool(t) for t in tools], **kwargs}}
            )

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            raise RuntimeError("use the asynchronous Kernel runtime")

        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            if guard:
                guard()
            payload = {**self.bound, **kwargs}
            if stop:
                payload["stop"] = stop
            # LangChain internal options must not leak to the provider API.
            payload.pop("ls_structured_output_format", None)
            data = await gateway.chat(role, convert_to_openai_messages(messages), task_id, **payload)
            if guard:
                guard()
            message = data["choices"][0]["message"]
            calls = [
                {
                    "name": c["function"]["name"],
                    "args": json.loads(c["function"]["arguments"]),
                    "id": c["id"],
                    "type": "tool_call",
                }
                for c in message.get("tool_calls", [])
            ]
            return ChatResult(
                generations=[
                    ChatGeneration(message=AIMessage(content=message.get("content") or "", tool_calls=calls))
                ],
                llm_output=data.get("usage", {}),
            )

    return RoutedChatModel()


class DeepAgentRuntime:
    """One durable graph thread per task, with Kernel-gated external tools."""

    def __init__(self, gateway):
        self.gateway = gateway

    async def run(self, worker, task):
        import asyncio
        from hashlib import sha256

        from deepagents import create_deep_agent
        from langchain.agents import create_agent
        from langchain.tools import ToolRuntime
        from langchain_core.tools import tool
        from langgraph.errors import GraphRecursionError
        from langgraph.types import Command, interrupt
        from sqlalchemy import select

        from niucai.agents.checkpointer import DatabaseSaver
        from niucai.control.tasks import require
        from niucai.domain.schemas import ActionSpec
        from niucai.storage.db import Action, Task

        token, task_id = task.run_token, task.id
        if task.checkpoint.get("proposal"):
            raise ValueError(
                "legacy structured proposal exists; finish it with NIUCAI_RUNTIME=structured first"
            )
        lock = asyncio.Lock()

        def guard():
            with worker.db.sessions() as s:
                worker.actions.check_task(require(s, Task, task_id), token)

        @tool
        async def execute_action(spec: ActionSpec, runtime: ToolRuntime) -> str:
            """Execute a typed computer action through Kernel policy and approval.

            Read status/result before deciding the next action. FAILED allows
            observation and correction; UNKNOWN requires human inspection.
            """
            async with lock:
                guard()
                # A model can reuse call IDs across turns. The persisted AI message
                # ID distinguishes turns and child graphs while remaining stable on replay.
                message_id = runtime.state["messages"][-1].id
                call_key = sha256(f"{message_id}:{runtime.tool_call_id}".encode()).hexdigest()
                key = f"{task_id}:graph:{call_key}"
                with worker.db.sessions() as s:
                    current = require(s, Task, task_id)
                    existing = s.scalar(select(Action).where(Action.idempotency_key == key))
                if not existing and current.current_step >= current.checkpoint.get(
                    "budget_limit", worker.settings.max_steps
                ):
                    interrupt({"reason": "step budget exhausted"})
                    # Kernel resume increases the budget; the model cannot change it.
                try:
                    action = worker.actions.propose(task_id, token, spec, key)
                except (PermissionError, ValueError) as exc:
                    return json.dumps({"status": "FAILED", "error": str(exc)[:500]})
                while True:
                    action = await worker.actions.execute(action.id, token)
                    if action.status not in {"WAITING_APPROVAL", "UNKNOWN"}:
                        break
                    interrupt({"reason": action.status, "action_id": action.id})
                    # Resume never grants approval itself: re-read the Kernel journal.
                with worker.db.sessions() as s:
                    current = require(s, Task, task_id)
                counted = current.checkpoint.get("counted_actions", [])
                if action.id not in counted:
                    worker.tasks.update(
                        task_id,
                        token,
                        current_step=current.current_step + 1,
                        checkpoint={
                            **current.checkpoint,
                            "counted_actions": [*counted, action.id],
                            "last_action_id": action.id,
                            "last_result": action.result,
                            "last_action_status": action.status,
                        },
                    )
                return json.dumps(
                    {"action_id": action.id, "status": action.status, "result": action.result},
                    ensure_ascii=False,
                )

        @tool
        async def update_plan(plan: Plan) -> str:
            """Persist an initial or revised task plan when feedback changes the approach."""
            guard()
            worker.tasks.update(task_id, token, plan=plan.model_dump(mode="json"))
            return "Plan saved."

        @tool
        async def wait_for_human(reason: str) -> str:
            """Pause for login, clarification or manual intervention; resume this session later."""
            guard()
            reply = interrupt({"reason": "agent requested human", "detail": reason[:4000]})
            guard()
            return json.dumps({"human_resumed": True, "reply": reply})

        package = worker.context.compile(task_id)
        if not task.plan:
            plan = await self.gateway.structured(Role.PLANNER, package, Plan, task_id)
            worker.tasks.update(task_id, token, plan=plan.model_dump(mode="json"))
            package = worker.context.compile(task_id)
        tools = [execute_action, update_plan, wait_for_human]
        prompt = (
            package["policies"]
            + " Use execute_action for every external computer/browser/file/shell operation. "
            "Built-in filesystem tools are virtual scratch space, not the computer workspace. "
            "Use update_plan to revise the plan when feedback changes the approach. "
            "React to tool feedback in this session; do not output action proposals as JSON. "
            "Use wait_for_human if blocked. A final answer completes your work. "
            "Kernel context at this run's start (later tool feedback supersedes observations): "
            + json.dumps(package, ensure_ascii=False)
        )
        # An explicitly compiled child inherits the parent's saver. Default raw
        # DeepAgents children are not checkpointed and can replan on interrupt replay.
        child = create_agent(
            model=gateway_model(self.gateway, Role.EXECUTOR, task_id, guard),
            tools=tools,
            system_prompt=prompt,
            checkpointer=True,
        )
        graph = create_deep_agent(
            model=gateway_model(self.gateway, Role.EXECUTOR, task_id, guard),
            tools=tools,
            checkpointer=DatabaseSaver(worker.db, task_id, token),
            system_prompt=prompt,
            subagents=[
                {
                    "name": "general-purpose",
                    "description": "A delegated worker using Kernel-gated tools.",
                    "runnable": child,
                }
            ],
        )
        config = {"configurable": {"thread_id": task_id}, "recursion_limit": 200}
        snapshot = await graph.aget_state(config)
        if snapshot.interrupts:
            inputs = Command(
                resume={
                    i.id: "Kernel resumed; re-check policy and action journal." for i in snapshot.interrupts
                }
            )
        elif snapshot.values:
            inputs = None  # Resume unfinished nodes or recover a persisted final answer.
        else:
            inputs = {
                "messages": [{"role": "user", "content": "Execute the task in the current Kernel context."}]
            }
        try:
            result = await graph.ainvoke(inputs, config=config)
        except GraphRecursionError:
            return {"status": "WAITING_HUMAN", "checkpoint": {"reason": "graph budget exhausted"}}
        interrupts = result.get("__interrupt__", ())
        if interrupts:
            return {"status": "WAITING_HUMAN", "checkpoint": dict(interrupts[0].value)}
        return {"status": "COMPLETED", "checkpoint": {"result": result["messages"][-1].content}}
