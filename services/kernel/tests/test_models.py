import json

import httpx
import pytest
from sqlalchemy import select

from niucai.agents.deepagent_adapter import DeepAgentRuntime
from niucai.agents.runtime import StructuredRuntime
from niucai.domain.schemas import Role
from niucai.models.gateway import ModelGateway, ModelRouter
from niucai.storage.db import Event


async def test_gateway_role_routing_and_usage(setup, tmp_path):
    db, settings, _, _ = setup
    config = tmp_path / "models.yaml"
    config.write_text('roles:\n  executor: {model: "test/model", tier: STANDARD, reasoning: medium}\n')
    settings.openrouter_api_key = "secret"
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": json.dumps({"kind": "complete", "explanation": "done"})}}
                ],
                "usage": {"total_tokens": 20, "cost": 0.001},
            },
        )

    gateway = ModelGateway(
        db, settings, ModelRouter(config), httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    result = await StructuredRuntime(gateway).decide({"policies": "policy"}, "task-1")
    assert result.kind == "complete"
    assert requests[0]["model"] == "test/model"
    assert requests[0]["reasoning"] == {"effort": "medium"}
    with db.sessions() as s:
        event = s.scalar(select(Event).where(Event.type == "model.completed"))
        assert event.data["usage"]["total_tokens"] == 20
        assert event.data["usage"]["cost"] == 0.001
    await gateway.close()


async def test_gateway_transient_retry(setup, tmp_path, monkeypatch):
    db, settings, _, _ = setup
    config = tmp_path / "models.yaml"
    config.write_text('roles:\n  fast: {model: "test/model"}\n')
    settings.openrouter_api_key = "secret"
    attempts = []

    def handler(request):
        attempts.append(request)
        if len(attempts) == 1:
            return httpx.Response(429, json={"error": "rate limited"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    async def immediate(_):
        pass

    monkeypatch.setattr("niucai.models.gateway.asyncio.sleep", immediate)
    gateway = ModelGateway(
        db, settings, ModelRouter(config), httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    assert (await gateway.chat(Role.FAST, [], "test"))["choices"][0]["message"]["content"] == "ok"
    assert len(attempts) == 2
    await gateway.close()


def test_missing_model_fails_before_network(tmp_path):
    config = tmp_path / "models.yaml"
    config.write_text('roles:\n  fast: {model: ""}\n')
    with pytest.raises(ValueError, match="configure model role"):
        ModelRouter(config).resolve(Role.FAST)


async def test_real_deepagents_adapter_uses_kernel_gateway():
    pytest.importorskip("deepagents")

    class FakeGateway:
        calls = []

        async def chat(self, role, messages, task_id, **extra):
            self.calls.append((role, messages, task_id, extra))
            return {
                "choices": [{"message": {"content": json.dumps({"kind": "complete", "explanation": "done"})}}]
            }

    gateway = FakeGateway()
    result = await DeepAgentRuntime(gateway).decide({"policies": "policy", "goal": "test"}, "test")
    assert result.kind == "complete"
    assert gateway.calls[0][0] == Role.EXECUTOR
    assert gateway.calls[0][2] == "test"
    assert "tools" in gateway.calls[0][3]
