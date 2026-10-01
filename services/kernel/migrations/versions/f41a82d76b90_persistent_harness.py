"""Persist LangGraph checkpoints and pending writes in the Kernel database."""

import sqlalchemy as sa
from alembic import op

revision = "f41a82d76b90"
down_revision = "c67565bea646"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "harness_checkpoints",
        sa.Column("thread_id", sa.String(200), nullable=False),
        sa.Column("namespace", sa.String(1000), nullable=False),
        sa.Column("checkpoint_id", sa.String(100), nullable=False),
        sa.Column("parent_id", sa.String(100)),
        sa.Column("payload_type", sa.String(30), nullable=False),
        sa.Column("payload", sa.LargeBinary(), nullable=False),
        sa.Column("metadata_type", sa.String(30), nullable=False),
        sa.Column("metadata_payload", sa.LargeBinary(), nullable=False),
        sa.PrimaryKeyConstraint("thread_id", "namespace", "checkpoint_id"),
    )
    op.create_table(
        "harness_writes",
        sa.Column("thread_id", sa.String(200), nullable=False),
        sa.Column("namespace", sa.String(1000), nullable=False),
        sa.Column("checkpoint_id", sa.String(100), nullable=False),
        sa.Column("task_id", sa.String(200), nullable=False),
        sa.Column("idx", sa.Integer(), nullable=False),
        sa.Column("channel", sa.String(200), nullable=False),
        sa.Column("payload_type", sa.String(30), nullable=False),
        sa.Column("payload", sa.LargeBinary(), nullable=False),
        sa.PrimaryKeyConstraint("thread_id", "namespace", "checkpoint_id", "task_id", "idx"),
    )


def downgrade():
    op.drop_table("harness_writes")
    op.drop_table("harness_checkpoints")
