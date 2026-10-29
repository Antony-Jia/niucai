from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import (
    JSON,
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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


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
