from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING_HUMAN = "WAITING_HUMAN"
    WAITING_BOT = "WAITING_BOT"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class Role(StrEnum):
    PLANNER = "planner"
    EXECUTOR = "executor"
    FAST = "fast"
    BROWSER = "browser"
    VISION = "vision"
    SUMMARIZER = "summarizer"
    GUARD = "guard"
    MEMORY = "memory"


class TaskCreate(Strict):
    title: str = Field(min_length=1, max_length=200)
    goal: str = Field(min_length=1, max_length=16000)
    computer_id: str | None = None
    agent_id: str = "main"


class ComputerCreate(Strict):
    name: str = Field(min_length=1, max_length=200)
    kind: Literal["linux", "android"] = "linux"


class MemoryCreate(Strict):
    kind: Literal["episodic", "semantic"]
    content: str = Field(min_length=1, max_length=16000)
    tags: list[str] = Field(default_factory=list, max_length=20)
    task_id: str | None = None
    agent_id: str | None = None


class ResourcePolicy(Strict):
    strategy: Literal["fixed", "pool"] = "pool"
    fixed_node_id: str | None = None
    required_capabilities: list[str] = Field(default_factory=list, max_length=32)
    workspace: Literal["managed"] = "managed"
    max_concurrency: int = Field(default=1, ge=1, le=32)
    offline_wait_seconds: int = Field(default=900, ge=1, le=86400)

    @model_validator(mode="after")
    def fixed_target(self):
        if (self.strategy == "fixed") != bool(self.fixed_node_id):
            raise ValueError("fixed strategy requires exactly one fixed_node_id")
        return self


class OutputContract(Strict):
    require_text: bool = True
    min_files: int = Field(default=0, ge=0, le=16)
    description: str = Field(default="", max_length=4000)


class AgentDefinition(Strict):
    runtime: Literal["pi", "deepagents", "structured"] = "pi"
    label: str = Field(default="", max_length=200)
    description: str = Field(default="", max_length=2000)
    instructions: str = Field(default="", max_length=16000)
    model_roles: dict[str, Role] = Field(default_factory=dict)
    tools: list[str] = Field(default_factory=list, max_length=100)
    default_computer_id: str | None = None
    allowed_nodes: list[str] | None = Field(default_factory=list)
    memory_scope: Literal["own", "own+shared", "shared"] = "own+shared"
    enabled: bool = True
    archived: bool = False
    definition_version: int = Field(default=1, ge=1)
    resource_policy: ResourcePolicy = Field(default_factory=ResourcePolicy)
    output_contract: OutputContract = Field(default_factory=OutputContract)
    delegates: list[str] | None = None

    @model_validator(mode="after")
    def authorized_target(self):
        target = self.resource_policy.fixed_node_id
        if target and self.allowed_nodes is not None and target not in self.allowed_nodes:
            raise ValueError("fixed node must be in allowed_nodes")
        return self


class AgentCreate(Strict):
    id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]{0,99}$")
    name: str = Field(min_length=1, max_length=200)
    definition: AgentDefinition = Field(default_factory=AgentDefinition)


class Navigate(Strict):
    type: Literal["browser.navigate"]
    url: HttpUrl


class Snapshot(Strict):
    type: Literal["browser.snapshot"]


class Click(Strict):
    type: Literal["browser.click"]
    ref: str = Field(min_length=1, max_length=100)
    button: Literal["left", "right"] = "left"


class Fill(Strict):
    type: Literal["browser.fill"]
    ref: str = Field(min_length=1, max_length=100)
    text: str = Field(max_length=16000)


class Scroll(Strict):
    type: Literal["browser.scroll"]
    x: int = Field(default=0, ge=-5000, le=5000)
    y: int = Field(ge=-5000, le=5000)


class Screenshot(Strict):
    type: Literal["browser.screenshot"]


class ReadFile(Strict):
    type: Literal["files.read"]
    path: str = Field(min_length=1, max_length=1024)


class WriteFile(Strict):
    type: Literal["files.write"]
    path: str = Field(min_length=1, max_length=1024)
    content: str = Field(max_length=100000)


class Shell(Strict):
    type: Literal["shell.exec"]
    argv: list[str] = Field(min_length=1, max_length=100)


ActionSpec = Annotated[
    Navigate | Snapshot | Click | Fill | Scroll | Screenshot | ReadFile | WriteFile | Shell,
    Field(discriminator="type"),
]


class Step(Strict):
    id: str
    type: Literal["research", "browser", "files", "shell", "reasoning"]
    description: str = Field(max_length=1000)
    status: Literal["pending", "completed"] = "pending"


class Plan(Strict):
    goal: str
    steps: list[Step] = Field(max_length=30)


class Decision(Strict):
    kind: Literal["action", "complete", "wait"]
    explanation: str = Field(max_length=4000)
    action: ActionSpec | None = None

    def validate_action(self):
        if (self.kind == "action") != (self.action is not None):
            raise ValueError("action is required only for action decisions")
        return self


class ActionCreate(Strict):
    task_id: str
    spec: ActionSpec
    idempotency_key: str = Field(min_length=1, max_length=200)


class ApprovalDecision(Strict):
    note: str = Field(default="", max_length=1000)
