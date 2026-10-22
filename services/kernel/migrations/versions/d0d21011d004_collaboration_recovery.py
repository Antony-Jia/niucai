"""Forward upgrade for existing multi-Bot databases and truthful recovery metadata."""

import sqlalchemy as sa
from alembic import op

revision = "d0d21011d004"
down_revision = "c9d21011d003"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("work_items") as batch:
        batch.alter_column("conversation_id", existing_type=sa.String(36), nullable=True)
    # Dedicated workflow inputs carry their workflow identity. Ordinary chat roots
    # have human inputs without that binding; do not detach dedicated conversations.
    op.execute("""
        UPDATE work_items SET conversation_id = NULL
        WHERE parent_work_item_id IS NULL AND conversation_id IS NOT NULL
        AND EXISTS (SELECT 1 FROM messages m WHERE m.task_id = work_items.task_id
            AND m.conversation_id = work_items.conversation_id
            AND m.role = 'user' AND m.source = 'USER' AND m.workflow_id IS NULL)
        AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.task_id = work_items.task_id
            AND m.role = 'user' AND m.workflow_id = work_items.workflow_id)
    """)
    with op.batch_alter_table("handoffs") as batch:
        batch.add_column(sa.Column("human_wait_started_at", sa.DateTime(), nullable=True))
    op.execute("""
        UPDATE handoffs SET human_wait_started_at =
            (SELECT t.updated_at FROM tasks t WHERE t.id = handoffs.to_task_id)
        WHERE status NOT IN ('REPLIED', 'COMPLETED', 'FAILED', 'CANCELLED', 'TIMED_OUT')
        AND EXISTS (SELECT 1 FROM tasks t WHERE t.id = handoffs.to_task_id AND t.status = 'WAITING_HUMAN')
    """)
    with op.batch_alter_table("task_runs") as batch:
        # Existing runs retain NULL: no claim of historical process tracking.
        batch.add_column(sa.Column("exit_tracking_started_at", sa.DateTime(), nullable=True))
    runs = sa.table(
        "task_runs",
        sa.column("task_id"),
        sa.column("status"),
        sa.column("ended_at"),
        sa.column("process_exited_at"),
    )
    tasks = sa.table("tasks", sa.column("id"), sa.column("checkpoint", sa.JSON()))
    non_pi = sa.exists(
        sa.select(tasks.c.id).where(
            tasks.c.id == runs.c.task_id,
            sa.or_(
                tasks.c.checkpoint["runtime"].as_string().is_(None),
                tasks.c.checkpoint["runtime"].as_string() != "pi",
            ),
        )
    )
    op.execute(
        runs.update()
        .where(
            sa.or_(runs.c.process_exited_at == runs.c.ended_at, sa.and_(runs.c.status == "EXPIRED", non_pi))
        )
        .values(process_exited_at=None)
    )


def downgrade():
    with op.batch_alter_table("task_runs") as batch:
        batch.drop_column("exit_tracking_started_at")
    with op.batch_alter_table("handoffs") as batch:
        batch.drop_column("human_wait_started_at")
