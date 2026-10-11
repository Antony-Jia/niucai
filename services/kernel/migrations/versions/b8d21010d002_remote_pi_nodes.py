"""Remote Pi execution nodes and independently leased child jobs."""

import sqlalchemy as sa
from alembic import op

revision = "b8d21010d002"
down_revision = "a7c21010d001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "remote_nodes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("enabled", sa.Integer(), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime()),
        sa.Column("capabilities", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "remote_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("parent_task_id", sa.String(36), sa.ForeignKey("tasks.id")),
        sa.Column("idempotency_key", sa.String(200), nullable=False, unique=True),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("allowed_nodes", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("node_id", sa.String(36), sa.ForeignKey("remote_nodes.id")),
        sa.Column("attempt_id", sa.String(36)),
        sa.Column("lease_until", sa.DateTime()),
        sa.Column("session_id", sa.String(200)),
        sa.Column("event_sequence", sa.Integer(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_remote_jobs_parent_task_id", "remote_jobs", ["parent_task_id"])
    op.create_index("ix_remote_jobs_status", "remote_jobs", ["status"])


def downgrade():
    op.drop_table("remote_jobs")
    op.drop_table("remote_nodes")
