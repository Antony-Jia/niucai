from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field, model_validator

from niucai.control.cards import CardManager

Engine = Literal["pi", "codex", "claude"]
Profile = Literal["read", "workspace", "full"]


def not_null(model):
    if any(getattr(model, key) is None for key in model.model_fields_set):
        raise ValueError("updated fields cannot be null")
    return model


class BotFields(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    role: str = Field(default="", max_length=500)
    instructions: str = Field(default="", max_length=32000)
    tags: list[str] = Field(default_factory=list, max_length=20)
    model: str = Field(default="", max_length=200)
    permission_profile: Profile = "workspace"
    skills: list[str] = Field(default_factory=list, max_length=32)
    mcp_servers: list[str] = Field(default_factory=list, max_length=32)
    requires_approval: bool = False
    concurrency: int = Field(default=1, ge=1, le=16)


class BotCreate(BotFields):
    name: str = Field(min_length=1, max_length=60)
    node_id: str = Field(min_length=1, max_length=36)
    engine: Engine


class BotUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str | None = Field(default=None, min_length=1, max_length=60)
    node_id: str | None = Field(default=None, min_length=1, max_length=36)
    engine: Engine | None = None
    role: str | None = Field(default=None, max_length=500)
    instructions: str | None = Field(default=None, max_length=32000)
    tags: list[str] | None = Field(default=None, max_length=20)
    model: str | None = Field(default=None, max_length=200)
    permission_profile: Profile | None = None
    skills: list[str] | None = Field(default=None, max_length=32)
    mcp_servers: list[str] | None = Field(default=None, max_length=32)
    requires_approval: bool | None = None
    concurrency: int | None = Field(default=None, ge=1, le=16)
    enabled: bool | None = None

    _not_null = model_validator(mode="after")(not_null)


class Budget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_attempts: int = Field(default=3, ge=1, le=20)
    max_minutes: int | None = Field(default=None, ge=1, le=1440)


class CardCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=200)
    goal: str = Field(default="", max_length=16000)
    acceptance: str = Field(default="", max_length=8000)
    conversation_id: str | None = Field(default=None, max_length=36)
    parent_card_id: str | None = Field(default=None, max_length=36)
    assignee_bot_id: str | None = Field(default=None, max_length=36)
    required_tags: list[str] = Field(default_factory=list, max_length=20)
    depends_on: list[str] = Field(default_factory=list, max_length=32)
    priority: int = Field(default=0, ge=-100, le=100)
    review: Literal["none", "llm"] = "none"
    budget: Budget = Field(default_factory=Budget)
    workdir: str = Field(default="", max_length=500)
    draft: bool = False


class CardUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    expected_version: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    goal: str | None = Field(default=None, max_length=16000)
    acceptance: str | None = Field(default=None, max_length=8000)
    assignee_bot_id: str | None = Field(default=None, max_length=36)
    required_tags: list[str] | None = Field(default=None, max_length=20)
    depends_on: list[str] | None = Field(default=None, max_length=32)
    priority: int | None = Field(default=None, ge=-100, le=100)
    review: Literal["none", "llm"] | None = None
    budget: Budget | None = None
    workdir: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def nullable(self):
        # assignee_bot_id 可显式置空（转入公共池），其余字段不能为 null。
        if any(getattr(self, k) is None for k in self.model_fields_set - {"assignee_bot_id"}):
            raise ValueError("updated fields cannot be null")
        return self


class CardAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int | None = Field(default=None, ge=1)
    body: str = Field(default="", max_length=16000)


class CommentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    body: str = Field(min_length=1, max_length=16000)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)


def board_router(db, settings, auth):
    router = APIRouter()
    manager = CardManager(db, settings)

    @router.get("/api/bots", dependencies=auth)
    def bots():
        return manager.bots()

    @router.post("/api/bots", dependencies=auth, status_code=201)
    def create_bot(request: BotCreate):
        return manager.create_bot(request.model_dump())

    @router.patch("/api/bots/{bot_id}", dependencies=auth)
    def update_bot(bot_id: str, request: BotUpdate):
        return manager.update_bot(bot_id, request.model_dump(exclude_unset=True))

    @router.delete("/api/bots/{bot_id}", dependencies=auth)
    def delete_bot(bot_id: str):
        return manager.delete_bot(bot_id)

    @router.get("/api/board", dependencies=auth)
    def board(conversation_id: str | None = None):
        return manager.board(conversation_id)

    @router.post("/api/cards", dependencies=auth, status_code=201)
    def create_card(request: CardCreate):
        return manager.create_card(request.model_dump())

    @router.get("/api/cards/{card_id}", dependencies=auth)
    def card(card_id: str):
        return manager.get(card_id)

    @router.patch("/api/cards/{card_id}", dependencies=auth)
    def update_card(card_id: str, request: CardUpdate):
        changes = request.model_dump(exclude_unset=True)
        return manager.update_card(card_id, changes.pop("expected_version"), changes)

    @router.post("/api/cards/{card_id}/comments", dependencies=auth, status_code=201)
    def comment(card_id: str, request: CommentCreate):
        key = f"user:{request.idempotency_key}" if request.idempotency_key else None
        return manager.comment(card_id, request.body, key=key)

    @router.post("/api/cards/{card_id}/{operation}", dependencies=auth)
    def action(
        card_id: str,
        operation: Literal["ready", "approve", "cancel", "accept", "reject", "answer", "reopen"],
        request: CardAction,
    ):
        return manager.action(card_id, operation, request.expected_version, request.body)

    return router
