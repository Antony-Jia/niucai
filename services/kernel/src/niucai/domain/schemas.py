from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING_HUMAN = "WAITING_HUMAN"
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
