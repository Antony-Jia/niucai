"""Persistent Bot identities, collaboration inbox, work items and scheduling."""

import sqlalchemy as sa
from alembic import op

revision = "c9d21011d003"
down_revision = "b8d21010d002"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("task_runs") as batch:
        batch.add_column(sa.Column("process_exited_at", sa.DateTime()))
    with op.batch_alter_table("remote_jobs") as batch:
        batch.add_column(sa.Column("input_artifacts", sa.JSON(), nullable=False, server_default="[]"))
    with op.batch_alter_table("memories") as batch:
        batch.add_column(sa.Column("agent_id", sa.String(100)))
        batch.create_foreign_key("fk_memory_agent", "agents", ["agent_id"], ["id"])
        batch.create_index("ix_memories_agent_id", ["agent_id"])
    with op.batch_alter_table("messages") as batch:
        for name, length in [
            ("sender_agent_id", 100),
            ("handoff_id", 36),
            ("workflow_id", 36),
            ("work_item_id", 36),
            ("routine_id", 36),
        ]:
            batch.add_column(sa.Column(name, sa.String(length)))
        batch.add_column(sa.Column("source", sa.String(20), nullable=False, server_default="USER"))
        batch.add_column(sa.Column("kind", sa.String(20), nullable=False, server_default="INSTRUCTION"))
    with op.batch_alter_table("artifacts") as batch:
        batch.add_column(sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
        batch.add_column(sa.Column("content_hash", sa.String(64)))
    op.create_table(
        "bot_control",
        sa.Column("id", sa.String(length=20), primary_key=True, nullable=False),
        sa.Column("stopped", sa.Integer(), nullable=False),
    )
    op.create_table(
        "tool_receipts",
        sa.Column("id", sa.String(length=200), primary_key=True, nullable=False),
        sa.Column("task_id", sa.String(length=36), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
    )
    op.create_table(
        "workflows",
        sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
        sa.Column(
            "origin_conversation_id", sa.String(length=36), sa.ForeignKey("conversations.id"), nullable=False
        ),
        sa.Column("owner_agent_id", sa.String(length=100), sa.ForeignKey("agents.id"), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("authorization", sa.JSON(), nullable=False),
        sa.Column("limits", sa.JSON(), nullable=False),
        sa.Column("routes", sa.JSON(), nullable=False),
        sa.Column("handoff_count", sa.Integer(), nullable=False),
        sa.Column("model_turns", sa.Integer(), nullable=False),
        sa.Column("revision_count", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_workflows_status", "workflows", ["status"], unique=False)
    op.create_table(
        "work_items",
        sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
        sa.Column("workflow_id", sa.String(length=36), sa.ForeignKey("workflows.id"), nullable=False),
        sa.Column("parent_work_item_id", sa.String(length=36), sa.ForeignKey("work_items.id"), nullable=True),
        sa.Column("assignee_agent_id", sa.String(length=100), sa.ForeignKey("agents.id"), nullable=False),
        sa.Column("conversation_id", sa.String(length=36), sa.ForeignKey("conversations.id"), nullable=True),
        sa.Column("task_id", sa.String(length=36), sa.ForeignKey("tasks.id"), nullable=True),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("output_requirements", sa.Text(), nullable=False),
        sa.Column("wait", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("artifacts", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("labels", sa.JSON(), nullable=False),
        sa.Column("due_at", sa.DateTime(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("task_id"),
        sa.UniqueConstraint("conversation_id"),
    )
    op.create_index("ix_work_items_status", "work_items", ["status"], unique=False)
    op.create_index("ix_work_items_workflow_id", "work_items", ["workflow_id"], unique=False)
    op.create_table(
        "handoffs",
        sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
        sa.Column("workflow_id", sa.String(length=36), sa.ForeignKey("workflows.id"), nullable=False),
        sa.Column("from_work_item_id", sa.String(length=36), sa.ForeignKey("work_items.id"), nullable=False),
        sa.Column("to_work_item_id", sa.String(length=36), sa.ForeignKey("work_items.id"), nullable=False),
        sa.Column("from_agent_id", sa.String(length=100), nullable=False),
        sa.Column("to_agent_id", sa.String(length=100), nullable=False),
        sa.Column("from_conversation_id", sa.String(length=36), nullable=False),
        sa.Column("to_conversation_id", sa.String(length=36), nullable=False),
        sa.Column("from_task_id", sa.String(length=36), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("to_task_id", sa.String(length=36), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("caused_by_handoff_id", sa.String(length=36), nullable=True),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("expects_reply", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("hop", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("reply", sa.JSON(), nullable=False),
        sa.Column("deadline", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index("ix_handoffs_status", "handoffs", ["status"], unique=False)
    op.create_index("ix_handoffs_workflow_id", "handoffs", ["workflow_id"], unique=False)
    op.create_table(
        "conversation_participants",
        sa.Column(
            "conversation_id",
            sa.String(length=36),
            sa.ForeignKey("conversations.id"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "agent_id", sa.String(length=100), sa.ForeignKey("agents.id"), primary_key=True, nullable=False
        ),
        sa.Column("role", sa.String(length=20), nullable=False),
    )
    op.create_table(
        "routines",
        sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
        sa.Column("agent_id", sa.String(length=100), sa.ForeignKey("agents.id"), nullable=False),
        sa.Column("conversation_id", sa.String(length=36), sa.ForeignKey("conversations.id"), nullable=True),
        sa.Column("schedule", sa.String(length=100), nullable=False),
        sa.Column("timezone", sa.String(length=100), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Integer(), nullable=False),
        sa.Column("last_run_at", sa.DateTime(), nullable=True),
        sa.Column("next_run_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_routines_next_run_at", "routines", ["next_run_at"], unique=False)
    op.create_table(
        "routine_runs",
        sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
        sa.Column("routine_id", sa.String(length=36), sa.ForeignKey("routines.id"), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(), nullable=False),
        sa.Column("workflow_id", sa.String(length=36), sa.ForeignKey("workflows.id"), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.UniqueConstraint("routine_id", "scheduled_at", name="uq_routine_schedule"),
    )
    op.execute("INSERT INTO bot_control (id, stopped) VALUES ('global', 0)")
    agents = sa.table("agents", sa.column("id", sa.String()), sa.column("definition", sa.JSON()))
    for row in op.get_bind().execute(sa.select(agents)).mappings():
        if row["id"] == "main":
            op.get_bind().execute(
                agents.update()
                .where(agents.c.id == "main")
                .values(
                    definition={
                        "runtime": "pi",
                        "label": "Main Agent",
                        "tools": ["*"],
                        "allowed_nodes": None,
                        "memory_scope": "own+shared",
                        **row["definition"],
                    }
                )
            )


def downgrade():
    with op.batch_alter_table("task_runs") as batch:
        batch.drop_column("process_exited_at")
    with op.batch_alter_table("remote_jobs") as batch:
        batch.drop_column("input_artifacts")
    for name in [
        "routine_runs",
        "routines",
        "conversation_participants",
        "handoffs",
        "work_items",
        "workflows",
        "tool_receipts",
        "bot_control",
    ]:
        op.drop_table(name)
    for table, names in [
        (
            "messages",
            ["sender_agent_id", "handoff_id", "workflow_id", "work_item_id", "routine_id", "source", "kind"],
        ),
        ("artifacts", ["version", "content_hash"]),
    ]:
        with op.batch_alter_table(table) as batch:
            for name in names:
                batch.drop_column(name)
    with op.batch_alter_table("memories") as batch:
        batch.drop_index("ix_memories_agent_id")
        batch.drop_constraint("fk_memory_agent", type_="foreignkey")
        batch.drop_column("agent_id")
