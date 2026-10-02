import asyncio
import time

import httpx
import yaml
from opentelemetry import trace
from pydantic import BaseModel, ConfigDict, Field, model_validator

from niucai.domain.schemas import Role
from niucai.storage.db import emit

tracer = trace.get_tracer("niucai.models")


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str
    tier: str = "STANDARD"
    reasoning: str | None = None
    context_window: int = Field(default=64000, ge=8192, le=2000000)
    max_tokens: int = Field(default=4096, ge=256, le=16000)

    @model_validator(mode="after")
    def valid_window(self):
        if self.max_tokens >= self.context_window:
            raise ValueError("max_tokens must be smaller than context_window")
        return self


class ModelRouter:
    def __init__(self, path):
        config = yaml.safe_load(path.read_text())
        self.profiles = {Role(k): Profile(**v) for k, v in config["roles"].items()}

    def resolve(self, role):
        profile = self.profiles[Role(role)]
        if not profile.model:
            raise ValueError(f"configure model role '{role}' in models.yaml")
        return profile


class ModelGateway:
    def __init__(self, db, settings, router, client=None):
        self.db, self.settings, self.router = db, settings, router
        self.client = client or httpx.AsyncClient(timeout=60)

    async def close(self):
        await self.client.aclose()

    async def chat(self, role, messages, task_id, **extra):
        profile = self.router.resolve(role)
        key = self.settings.openrouter_api_key.get_secret_value()
        if not key:
            raise ValueError("configure NIUCAI_OPENROUTER_API_KEY")
        request = {"model": profile.model, "messages": messages, "max_tokens": profile.max_tokens, **extra}
        if profile.reasoning:
            request["reasoning"] = {"effort": profile.reasoning}
        with self.db.sessions.begin() as s:
            emit(s, "model.requested", task_id, role=str(role), model=profile.model, tier=profile.tier)
        started = time.monotonic()
        with tracer.start_as_current_span("model.chat") as span:
            span.set_attribute("model.role", str(role))
            span.set_attribute("task.id", task_id)
            try:
                for attempt in range(3):
                    response = await self.client.post(
                        "https://openrouter.ai/api/v1/chat/completions",
                        json=request,
                        headers={"Authorization": f"Bearer {key}", "X-Title": "niucai"},
                    )
                    if response.status_code in {429, 502, 503, 504} and attempt < 2:
                        await asyncio.sleep(2**attempt)
                        continue
                    response.raise_for_status()
                    data = response.json()
                    with self.db.sessions.begin() as s:
                        emit(
                            s,
                            "model.completed",
                            task_id,
                            role=str(role),
                            model=profile.model,
                            latency_ms=int((time.monotonic() - started) * 1000),
                            usage=data.get("usage", {}),
                        )
                    return data
            except Exception as exc:
                with self.db.sessions.begin() as s:
                    emit(s, "model.failed", task_id, error=type(exc).__name__)
                raise

    async def structured(self, role, package, schema, task_id):
        import json

        data = await self.chat(
            role,
            [
                {
                    "role": "system",
                    "content": package["policies"] + " Return only JSON matching the supplied schema.",
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {"context": package, "schema": schema.model_json_schema()}, ensure_ascii=False
                    ),
                },
            ],
            task_id,
            response_format={"type": "json_object"},
        )
        return schema.model_validate_json(data["choices"][0]["message"]["content"])
