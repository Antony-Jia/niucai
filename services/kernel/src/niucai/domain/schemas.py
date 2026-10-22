from enum import StrEnum
from typing import Annotated, Any, Literal

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
    # None 表示可用全部已启用的技能 / MCP 服务器；列表按 fnmatch 模式收窄。
    skills: list[str] | None = Field(default=None, max_length=100)
    mcp_servers: list[str] | None = Field(default=None, max_length=100)

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


class PipelineStage(Strict):
    """依赖流程的一个阶段；depends_on 引用同一流程中先声明或后声明的阶段 key。"""

    key: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,39}$")
    agent_id: str = Field(min_length=1, max_length=100)
    goal: str = Field(min_length=1, max_length=16000)
    depends_on: list[str] = Field(default_factory=list, max_length=12)
    output_requirements: str = Field(default="", max_length=4000)


def pipeline_order(stages):
    """校验阶段图（唯一 key、依赖存在、无环）并返回拓扑顺序。"""
    if not 1 <= len(stages) <= 12:
        raise ValueError("pipeline requires 1-12 stages")
    by_key = {stage.key: stage for stage in stages}
    if len(by_key) != len(stages):
        raise ValueError("pipeline stage keys must be unique")
    order, state = [], {}

    def visit(stage):
        if state.get(stage.key) == "done":
            return
        if state.get(stage.key) == "visiting":
            raise ValueError("pipeline dependencies contain a cycle")
        state[stage.key] = "visiting"
        if len(set(stage.depends_on)) != len(stage.depends_on):
            raise ValueError(f"stage {stage.key} repeats a dependency")
        for key in stage.depends_on:
            if key not in by_key:
                raise ValueError(f"stage {stage.key} depends on unknown stage {key}")
            visit(by_key[key])
        state[stage.key] = "done"
        order.append(stage)

    for stage in stages:
        visit(stage)
    return order


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


class SkillRead(Strict):
    type: Literal["skills.read"]
    name: str = Field(min_length=1, max_length=100)


class McpCall(Strict):
    type: Literal["mcp.call"]
    server: str = Field(min_length=1, max_length=100)
    tool: str = Field(min_length=1, max_length=200)
    arguments: dict[str, Any] = Field(default_factory=dict)


ActionSpec = Annotated[
    Navigate
    | Snapshot
    | Click
    | Fill
    | Scroll
    | Screenshot
    | ReadFile
    | WriteFile
    | Shell
    | SkillRead
    | McpCall,
    Field(discriminator="type"),
]

NAME = r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}$"


class SkillCreate(Strict):
    name: str = Field(pattern=NAME)
    description: str = Field(default="", max_length=1000)
    content: str = Field(default="", max_length=200000)
    enabled: bool = True


class SkillPatch(Strict):
    name: str | None = Field(default=None, pattern=NAME)
    description: str | None = Field(default=None, max_length=1000)
    content: str | None = Field(default=None, max_length=200000)
    enabled: bool | None = None


class McpStdio(Strict):
    command: str = Field(min_length=1, max_length=1000)
    args: list[str] = Field(default_factory=list, max_length=100)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = Field(default=None, max_length=1024)


class McpHttp(Strict):
    url: HttpUrl
    headers: dict[str, str] = Field(default_factory=dict)


class McpServerCreate(Strict):
    name: str = Field(pattern=NAME)
    transport: Literal["stdio", "http"]
    config: dict[str, Any]
    enabled: bool = True
    auto_approve_read_only: bool = True

    @model_validator(mode="after")
    def transport_config(self):
        model = McpStdio if self.transport == "stdio" else McpHttp
        self.config = model.model_validate(self.config).model_dump(mode="json")
        return self


class McpServerPatch(Strict):
    name: str | None = Field(default=None, pattern=NAME)
    config: dict[str, Any] | None = None
    enabled: bool | None = None
    auto_approve_read_only: bool | None = None


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
