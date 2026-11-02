from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def now():
    return datetime.now(UTC).replace(tzinfo=None)


def uid():
    return str(uuid4())


class Base(DeclarativeBase):
    pass


class KernelInstance(Base):
    __tablename__ = "kernel_instance"
    id: Mapped[str] = mapped_column(String(20), primary_key=True, default="singleton")
    instance_id: Mapped[str] = mapped_column(String(36), default=uid)
    admission_enabled: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class WorkerHealth(Base):
    __tablename__ = "worker_health"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    details: Mapped[dict] = mapped_column(JSON, default=dict)


class HarnessCheckpoint(Base):
    __tablename__ = "harness_checkpoints"
    thread_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    namespace: Mapped[str] = mapped_column(String(1000), primary_key=True)
    checkpoint_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    parent_id: Mapped[str | None] = mapped_column(String(100))
    payload_type: Mapped[str] = mapped_column(String(30))
    payload: Mapped[bytes] = mapped_column(LargeBinary)
    metadata_type: Mapped[str] = mapped_column(String(30))
    metadata_payload: Mapped[bytes] = mapped_column(LargeBinary)


class HarnessWrite(Base):
    __tablename__ = "harness_writes"
    thread_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    namespace: Mapped[str] = mapped_column(String(1000), primary_key=True)
    checkpoint_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    idx: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel: Mapped[str] = mapped_column(String(200))
    payload_type: Mapped[str] = mapped_column(String(30))
    payload: Mapped[bytes] = mapped_column(LargeBinary)


class BrowserSession(Base):
    __tablename__ = "browser_sessions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    key_fingerprint: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    title: Mapped[str] = mapped_column(String(200))
    agent_id: Mapped[str] = mapped_column(String(100), default="main")
    computer_id: Mapped[str | None] = mapped_column(ForeignKey("computers.id"))
    next_sequence: Mapped[int] = mapped_column(Integer, default=0)
    # mode: agent|chat；autonomy: {"actions": bool, "remote": bool}（YOLO，委派会话继承）。
    settings: Mapped[dict] = mapped_column(JSON, default=dict)
    # Bot 协作 C3：本 Goal 是否启用裁判；空表示跟随全局开关（NIUCAI_JUDGE_ENABLED），False 为关闭。
    judge_enabled: Mapped[bool | None] = mapped_column(Boolean)
    # 软删除：用户从列表移除已结束的任务；审计、动作和卡片记录仍保留。
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    sequence: Mapped[int | None] = mapped_column(Integer)
    client_id: Mapped[str | None] = mapped_column(String(100))
    task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id"), index=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime)
    submission_id: Mapped[str | None] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20))
    sender_agent_id: Mapped[str | None] = mapped_column(String(100))
    handoff_id: Mapped[str | None] = mapped_column(String(36))
    workflow_id: Mapped[str | None] = mapped_column(String(36))
    work_item_id: Mapped[str | None] = mapped_column(String(36))
    routine_id: Mapped[str | None] = mapped_column(String(36))
    source: Mapped[str] = mapped_column(String(20), default="USER")
    kind: Mapped[str] = mapped_column(String(20), default="INSTRUCTION")
    content: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="COMPLETED")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    __table_args__ = (
        UniqueConstraint("conversation_id", "sequence", name="uq_message_sequence"),
        UniqueConstraint("conversation_id", "client_id", name="uq_message_client"),
    )


class GoalRequest(Base):
    """Goal 创建幂等键：主键即 client_id，并发的重复创建在数据库层只有一个能落库。"""

    __tablename__ = "goal_requests"
    client_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), unique=True)
    content_digest: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    conversation_id: Mapped[str | None] = mapped_column(ForeignKey("conversations.id"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    goal: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="PENDING", index=True)
    agent_id: Mapped[str] = mapped_column(String(100), default="main")
    computer_id: Mapped[str | None] = mapped_column(ForeignKey("computers.id"))
    plan: Mapped[dict] = mapped_column(JSON, default=dict)
    checkpoint: Mapped[dict] = mapped_column(JSON, default=dict)
    current_step: Mapped[int] = mapped_column(Integer, default=0)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    run_token: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime)
    # 软删除：同 Conversation.deleted_at。
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class Computer(Base):
    __tablename__ = "computers"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(30), default="linux")
    status: Mapped[str] = mapped_column(String(30), default="OFFLINE")
    control: Mapped[str] = mapped_column(String(30), default="AGENT")
    active_task_id: Mapped[str | None] = mapped_column(String(36))
    state: Mapped[dict] = mapped_column(JSON, default=dict)


class Agent(Base):
    __tablename__ = "agents"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    definition: Mapped[dict] = mapped_column(JSON, default=dict)


class Action(Base):
    __tablename__ = "actions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    agent_id: Mapped[str] = mapped_column(String(100))
    computer_id: Mapped[str | None] = mapped_column(ForeignKey("computers.id"))
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True)
    spec: Mapped[dict] = mapped_column(JSON)
    risk: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(30), default="PROPOSED")
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Approval(Base):
    __tablename__ = "approvals"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    action_id: Mapped[str] = mapped_column(ForeignKey("actions.id"), unique=True)
    status: Mapped[str] = mapped_column(String(30), default="PENDING")
    note: Mapped[str] = mapped_column(Text, default="")
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    type: Mapped[str] = mapped_column(String(100), index=True)
    task_id: Mapped[str | None] = mapped_column(String(36), index=True)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Audit(Base):
    __tablename__ = "audit_logs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    action_id: Mapped[str] = mapped_column(ForeignKey("actions.id"), index=True)
    outcome: Mapped[str] = mapped_column(String(30))
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Memory(Base):
    __tablename__ = "memories"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    kind: Mapped[str] = mapped_column(String(30))
    agent_id: Mapped[str | None] = mapped_column(ForeignKey("agents.id"), index=True)
    content: Mapped[str] = mapped_column(Text)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id"))
    # 白板记忆（P3）：kind=card 时 ref_id 为卡片，kind=goal 时为 Goal 会话；同一 ref 只保留一条。
    ref_id: Mapped[str | None] = mapped_column(String(36), index=True)
    bot_id: Mapped[str | None] = mapped_column(String(36))
    # kind=decision（C3）的结构化字段放这里而不是加列：检索是“按时间取最近 N 条再在内存里筛”，
    # 不需要按这些字段建索引；字段 scope / goal_id / question / answer / principle / card_id /
    # event_id / corrects / corrected_by 见 control/judge.py。其它 kind 为空。
    meta: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class JudgeRun(Base):
    """裁判运行（C3）：一次 card_block 取舍类提问对应一行，question_event_id 唯一保证同一个问题只裁决一次。"""

    __tablename__ = "judge_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    card_id: Mapped[str] = mapped_column(ForeignKey("cards.id"), index=True)
    conversation_id: Mapped[str | None] = mapped_column(String(36), index=True)
    question_event_id: Mapped[int] = mapped_column(ForeignKey("card_events.id"), unique=True)
    question: Mapped[str] = mapped_column(Text)
    # PENDING / DONE / FAILED / DISCARDED（用户抢先回答、卡片已不在等这个问题）
    status: Mapped[str] = mapped_column(String(12), default="PENDING", index=True)
    verdict: Mapped[str | None] = mapped_column(String(10))  # decided / returned / held
    record_ids: Mapped[list] = mapped_column(JSON, default=list)  # 提供给模型的 decision 记录
    cited_ids: Mapped[list] = mapped_column(JSON, default=list)  # 裁决引用的记录
    answer: Mapped[str] = mapped_column(Text, default="")
    explanation: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[str] = mapped_column(String(200), default="")
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    usage: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime)
    correction: Mapped[str | None] = mapped_column(Text)
    correction_record_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)


class Artifact(Base):
    __tablename__ = "artifacts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    path: Mapped[str] = mapped_column(Text)
    media_type: Mapped[str] = mapped_column(String(100))
    version: Mapped[int] = mapped_column(Integer, default=1)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class TaskRun(Base):
    __tablename__ = "task_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    status: Mapped[str] = mapped_column(String(30), default="RUNNING")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime)
    process_exited_at: Mapped[datetime | None] = mapped_column(DateTime)
    exit_tracking_started_at: Mapped[datetime | None] = mapped_column(DateTime, default=now)


class BotControl(Base):
    __tablename__ = "bot_control"
    id: Mapped[str] = mapped_column(String(20), primary_key=True, default="global")
    stopped: Mapped[int] = mapped_column(Integer, default=0)


class ToolReceipt(Base):
    __tablename__ = "tool_receipts"
    id: Mapped[str] = mapped_column(String(200), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"))
    payload: Mapped[dict] = mapped_column(JSON)
    result: Mapped[dict] = mapped_column(JSON)


class Workflow(Base):
    __tablename__ = "workflows"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    origin_conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"))
    owner_agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"))
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE", index=True)
    authorization: Mapped[dict] = mapped_column(JSON, default=dict)
    limits: Mapped[dict] = mapped_column(JSON, default=dict)
    routes: Mapped[list] = mapped_column(JSON, default=list)
    handoff_count: Mapped[int] = mapped_column(Integer, default=0)
    model_turns: Mapped[int] = mapped_column(Integer, default=0)
    revision_count: Mapped[int] = mapped_column(Integer, default=0)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class WorkItem(Base):
    __tablename__ = "work_items"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    workflow_id: Mapped[str] = mapped_column(ForeignKey("workflows.id"), index=True)
    parent_work_item_id: Mapped[str | None] = mapped_column(ForeignKey("work_items.id"))
    assignee_agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"))
    definition_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    # NULL for a root started from an ordinary chat; that chat is not an execution session.
    conversation_id: Mapped[str | None] = mapped_column(ForeignKey("conversations.id"), unique=True)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id"), unique=True)
    goal: Mapped[str] = mapped_column(Text)
    output_requirements: Mapped[str] = mapped_column(Text, default="")
    wait: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    artifacts: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(30), default="QUEUED", index=True)
    labels: Mapped[list] = mapped_column(JSON, default=list)
    due_at: Mapped[datetime | None] = mapped_column(DateTime)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class Handoff(Base):
    __tablename__ = "handoffs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    workflow_id: Mapped[str] = mapped_column(ForeignKey("workflows.id"), index=True)
    from_work_item_id: Mapped[str] = mapped_column(ForeignKey("work_items.id"))
    to_work_item_id: Mapped[str] = mapped_column(ForeignKey("work_items.id"))
    from_agent_id: Mapped[str] = mapped_column(String(100))
    to_agent_id: Mapped[str] = mapped_column(String(100))
    from_conversation_id: Mapped[str] = mapped_column(String(36))
    to_conversation_id: Mapped[str] = mapped_column(String(36))
    from_task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"))
    to_task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"))
    caused_by_handoff_id: Mapped[str | None] = mapped_column(String(36))
    kind: Mapped[str] = mapped_column(String(20), default="DELEGATE")
    expects_reply: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(30), default="ACCEPTED", index=True)
    hop: Mapped[int] = mapped_column(Integer)
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True)
    payload: Mapped[dict] = mapped_column(JSON)
    reply: Mapped[dict] = mapped_column(JSON, default=dict)
    deadline: Mapped[datetime] = mapped_column(DateTime)
    human_wait_started_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class ConversationParticipant(Base):
    __tablename__ = "conversation_participants"
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), primary_key=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(20))


class Routine(Base):
    __tablename__ = "routines"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"))
    conversation_id: Mapped[str | None] = mapped_column(ForeignKey("conversations.id"))
    schedule: Mapped[str] = mapped_column(String(100))
    timezone: Mapped[str] = mapped_column(String(100), default="Asia/Shanghai")
    prompt: Mapped[str] = mapped_column(Text)
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime)
    next_run_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class RoutineRun(Base):
    __tablename__ = "routine_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    routine_id: Mapped[str] = mapped_column(ForeignKey("routines.id"))
    scheduled_at: Mapped[datetime] = mapped_column(DateTime)
    workflow_id: Mapped[str | None] = mapped_column(ForeignKey("workflows.id"))
    status: Mapped[str] = mapped_column(String(30))
    __table_args__ = (UniqueConstraint("routine_id", "scheduled_at", name="uq_routine_schedule"),)


class RemoteNode(Base):
    __tablename__ = "remote_nodes"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(200))
    resource_type: Mapped[str] = mapped_column(
        String(30), default="unspecified", server_default="unspecified"
    )
    token_hash: Mapped[str] = mapped_column(String(64))
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime)
    capabilities: Mapped[dict] = mapped_column(JSON, default=dict)
    connection: Mapped[dict] = mapped_column(JSON, default=dict)
    platform: Mapped[str] = mapped_column(String(20), default="", server_default="")
    runner_version: Mapped[str] = mapped_column(String(50), default="", server_default="")
    slots: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    preview_url: Mapped[str] = mapped_column(String(500), default="", server_default="")
    paired_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class PairingCode(Base):
    """一次性节点配对码；只存哈希，兑换后生成节点令牌。"""

    __tablename__ = "pairing_codes"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    resource_type: Mapped[str] = mapped_column(String(30), default="unspecified")
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    used_at: Mapped[datetime | None] = mapped_column(DateTime)
    node_id: Mapped[str | None] = mapped_column(ForeignKey("remote_nodes.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class RemoteJob(Base):
    __tablename__ = "remote_jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    parent_task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True)
    engine: Mapped[str] = mapped_column(String(20), default="pi", server_default="pi")
    prompt: Mapped[str] = mapped_column(Text)
    input_artifacts: Mapped[list] = mapped_column(JSON, default=list)
    config_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    wait_state: Mapped[dict] = mapped_column(JSON, default=dict)
    allowed_nodes: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(30), default="WAITING_APPROVAL", index=True)
    node_id: Mapped[str | None] = mapped_column(ForeignKey("remote_nodes.id"))
    attempt_id: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime)
    session_id: Mapped[str | None] = mapped_column(String(200))
    event_sequence: Mapped[int] = mapped_column(Integer, default=0)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    # 白板链路：一张卡的每次执行就是一条 remote_jobs（Attempt）。
    card_id: Mapped[str | None] = mapped_column(ForeignKey("cards.id"), index=True)
    bot_id: Mapped[str | None] = mapped_column(ForeignKey("bots.id"), index=True)
    board_token_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class Bot(Base):
    """节点上的一个执行者：引擎 + 角色 + 权限档位；白板卡片的领取单位。"""

    __tablename__ = "bots"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(60), unique=True)
    node_id: Mapped[str] = mapped_column(ForeignKey("remote_nodes.id"), index=True)
    engine: Mapped[str] = mapped_column(String(20))
    role: Mapped[str] = mapped_column(String(500), default="")
    instructions: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[list] = mapped_column(JSON, default=list)
    model: Mapped[str] = mapped_column(String(200), default="")
    reasoning_effort: Mapped[str] = mapped_column(String(20), default="", server_default="")
    # 环境画像：{"probed": Runner 探测上报, "manual": 用户手填}；只放事实说明，绝不存凭据。
    environment: Mapped[dict] = mapped_column(JSON, default=dict)
    # 协作策略（C2）：can_delegate / delegate_to / accept_from / auto_approve_from 生效，其余只存储；
    # 缺省字段按 collab.POLICY_DEFAULTS 补齐。不属于 Runner 配置，改动不递增 config_version。
    collab_policy: Mapped[dict] = mapped_column(JSON, default=dict)
    permission_profile: Mapped[str] = mapped_column(String(20), default="workspace")
    skills: Mapped[list] = mapped_column(JSON, default=list)
    mcp_servers: Mapped[list] = mapped_column(JSON, default=list)
    requires_approval: Mapped[int] = mapped_column(Integer, default=0)
    concurrency: Mapped[int] = mapped_column(Integer, default=1)
    config_version: Mapped[int] = mapped_column(Integer, default=1)
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class Card(Base):
    """白板卡片；状态机见 control/cards.py，修改带 expected_version。"""

    __tablename__ = "cards"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    conversation_id: Mapped[str | None] = mapped_column(ForeignKey("conversations.id"), index=True)
    parent_card_id: Mapped[str | None] = mapped_column(ForeignKey("cards.id"))
    title: Mapped[str] = mapped_column(String(200))
    goal: Mapped[str] = mapped_column(Text, default="")
    acceptance: Mapped[str] = mapped_column(Text, default="")
    assignee_bot_id: Mapped[str | None] = mapped_column(ForeignKey("bots.id"), index=True)
    required_tags: Mapped[list] = mapped_column(JSON, default=list)
    depends_on: Mapped[list] = mapped_column(JSON, default=list)
    # 输入产物：[{"artifact_id": ...}]，领取时随快照下发到 inputs/<来源卡片 id>/。
    inputs: Mapped[list] = mapped_column(JSON, default=list)
    # 资源声明：[{"resource": "browser:<node>", "mode": "shared" | "exclusive"}]，终态时释放。
    claims: Mapped[list] = mapped_column(JSON, default=list)
    # 等待其它卡片（C2）：{"cards": [...], "mode": "all" | "any", "until": ISO 时间, ...}；
    # RUNNING 时是 Attempt 内已登记的等待，WAITING 时是挂起条件，唤醒后保留到下次领取写入提示。
    waiting_on: Mapped[dict | None] = mapped_column(JSON)
    # 委派深度：用户建的卡为 0，Bot 每委派一层 +1。
    depth: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # 节点本地目录（如某个仓库）；Runner 只接受节点白名单内的目录，空串为节点托管工作区。
    workdir: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[str] = mapped_column(String(30), default="READY", index=True)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    review: Mapped[str] = mapped_column(String(10), default="none")
    created_by: Mapped[str] = mapped_column(String(60), default="user")
    current_job_id: Mapped[str | None] = mapped_column(String(36))
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    budget: Mapped[dict] = mapped_column(JSON, default=dict)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class OrchestratorRun(Base):
    """编排 LLM 的一次运行：触发原因、工具调用、输出与用量；每个 Goal 同时最多一个 RUNNING。"""

    __tablename__ = "orchestrator_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="PENDING", index=True)
    triggers: Mapped[list] = mapped_column(JSON, default=list)
    steps: Mapped[list] = mapped_column(JSON, default=list)
    output: Mapped[str] = mapped_column(Text, default="")
    usage: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)


class CardEvent(Base):
    """卡片时间线：只追加；(card_id, seq) 单调递增。"""

    __tablename__ = "card_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    card_id: Mapped[str] = mapped_column(ForeignKey("cards.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(20))
    actor: Mapped[str] = mapped_column(String(60))
    mentions: Mapped[list] = mapped_column(JSON, default=list)
    body: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    # 回复哪一条事件（card_events.id，同一 Goal 内）；可空。
    reply_to: Mapped[int | None] = mapped_column(ForeignKey("card_events.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    __table_args__ = (UniqueConstraint("card_id", "seq"),)


class BoardArtifact(Base):
    """Bot 经 artifact_put 上传的产物（内容存库，有大小上限），同一 Goal 内可读取或列入卡片 inputs。"""

    __tablename__ = "board_artifacts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    conversation_id: Mapped[str | None] = mapped_column(ForeignKey("conversations.id"), index=True)
    card_id: Mapped[str] = mapped_column(ForeignKey("cards.id"), index=True)
    bot_id: Mapped[str | None] = mapped_column(String(36))
    name: Mapped[str] = mapped_column(String(200))
    size: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    content: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


Index("ix_tasks_claim", Task.status, Task.lease_until, Task.created_at)
Index(
    "uq_conversation_active_task",
    Task.conversation_id,
    unique=True,
    sqlite_where=Task.status.not_in(["COMPLETED", "CANCELLED"]),
    postgresql_where=Task.status.not_in(["COMPLETED", "CANCELLED"]),
)


class Skill(Base):
    """Kernel 托管的技能说明包；所有 Bot 可读，按 Bot 定义的 skills 白名单收窄。"""

    __tablename__ = "skills"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    description: Mapped[str] = mapped_column(String(1000), default="")
    content: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class McpServer(Base):
    """Kernel 托管的 MCP 服务器；工具清单在刷新时缓存，调用经 ActionGateway。"""

    __tablename__ = "mcp_servers"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    transport: Mapped[str] = mapped_column(String(20))
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    tools: Mapped[list] = mapped_column(JSON, default=list)
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    auto_approve_read_only: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class ArtifactUpload(Base):
    __tablename__ = "artifact_uploads"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    job_id: Mapped[str] = mapped_column(ForeignKey("remote_jobs.id"), index=True)
    attempt_id: Mapped[str] = mapped_column(String(36))
    filename: Mapped[str] = mapped_column(String(200))
    content_hash: Mapped[str] = mapped_column(String(64))
    media_type: Mapped[str] = mapped_column(String(100))
    size: Mapped[int] = mapped_column(Integer)
    received: Mapped[int] = mapped_column(Integer, default=0)
    artifact_id: Mapped[str | None] = mapped_column(ForeignKey("artifacts.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    __table_args__ = (UniqueConstraint("job_id", "attempt_id", "filename"),)


class Database:
    def __init__(self, url: str):
        kwargs = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {}
        self.engine = create_engine(url, pool_pre_ping=True, **kwargs)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        if url.startswith("sqlite"):
            from sqlalchemy import event

            @event.listens_for(self.engine, "connect")
            def foreign_keys(connection, _):
                connection.execute("PRAGMA foreign_keys=ON")

    def create_schema(self):
        Base.metadata.create_all(self.engine)


def emit(session, event_type: str, task_id: str | None = None, **data):
    task = None
    if task_id and event_type.startswith(("task.", "action.", "approval.", "agent.", "model.")):
        from sqlalchemy import select

        from niucai.control.progress import journal_event, task_progress

        # Task locks precede the event allocation lock throughout the control plane.
        task = session.scalar(select(Task).where(Task.id == task_id).with_for_update())
        if task and journal_event(session, task, event_type, data):
            session.flush()
            data = {**data, "progress": task_progress(session, task)}
    if session.bind.dialect.name == "postgresql":
        from sqlalchemy import text

        # Serialize event allocation through commit so cursor replay cannot miss
        # a later-committing transaction with an earlier sequence number.
        session.execute(text("SELECT pg_advisory_xact_lock(68194210)"))
    from opentelemetry import trace

    span = trace.get_current_span().get_span_context()
    if span.is_valid:
        data = {**data, "trace_id": f"{span.trace_id:032x}", "span_id": f"{span.span_id:016x}"}
    session.add(Event(type=event_type, task_id=task_id, data=data))


def as_dict(row):
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}
