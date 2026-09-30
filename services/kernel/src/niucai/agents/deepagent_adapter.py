"""Optional DeepAgents harness. Its filesystem is virtual; no direct computer tools.

The gateway-backed model keeps provider selection, metrics and credentials in Kernel.
Kernel checkpoint is authoritative; the harness is rebuilt for each bounded decision.
"""

import json
from typing import Any

from niucai.domain.schemas import Decision, Plan, Role


def gateway_model(gateway, role, task_id):
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
            payload = {**self.bound, **kwargs}
            if stop:
                payload["stop"] = stop
            # LangChain internal options must not leak to the provider API.
            payload.pop("ls_structured_output_format", None)
            data = await gateway.chat(role, convert_to_openai_messages(messages), task_id, **payload)
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
    def __init__(self, gateway):
        self.gateway = gateway

    async def _run(self, package, task_id, role, schema):
        from deepagents import create_deep_agent

        graph = create_deep_agent(
            model=gateway_model(self.gateway, role, task_id),
            tools=[],
            system_prompt=package["policies"] + " Propose only; never execute external actions. "
            "Your final answer must be JSON matching this schema: " + json.dumps(schema.model_json_schema()),
        )
        state = await graph.ainvoke(
            {"messages": [{"role": "user", "content": json.dumps(package)}]}, config={"recursion_limit": 24}
        )
        content = state["messages"][-1].content
        if not isinstance(content, str):
            raise ValueError("harness returned unsupported content")
        return schema.model_validate_json(content)

    async def plan(self, package, task_id):
        return await self._run(package, task_id, Role.PLANNER, Plan)

    async def decide(self, package, task_id):
        return (await self._run(package, task_id, Role.EXECUTOR, Decision)).validate_action()
