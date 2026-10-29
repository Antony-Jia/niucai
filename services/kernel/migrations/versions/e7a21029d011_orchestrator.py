"""Orchestrator runs and board memories (kernel-lite P3)."""

import sqlalchemy as sa
from alembic import op

revision = "e7a21029d011"
down_revision = "d6e21029d010"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "orchestrator_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("conversation_id", sa.String(36), sa.ForeignKey("conversations.id"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("triggers", sa.JSON(), nullable=False),
        sa.Column("steps", sa.JSON(), nullable=False),
        sa.Column("output", sa.Text(), nullable=False),
        sa.Column("usage", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("lease_until", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_orchestrator_runs_conversation_id", "orchestrator_runs", ["conversation_id"])
    op.create_index("ix_orchestrator_runs_status", "orchestrator_runs", ["status"])
    op.add_column("memories", sa.Column("ref_id", sa.String(36), nullable=True))
    op.add_column("memories", sa.Column("bot_id", sa.String(36), nullable=True))
    op.create_index("ix_memories_ref_id", "memories", ["ref_id"])


def downgrade():
    op.drop_index("ix_memories_ref_id", table_name="memories")
    with op.batch_alter_table("memories") as batch:
        batch.drop_column("bot_id")
        batch.drop_column("ref_id")
    op.drop_index("ix_orchestrator_runs_status", table_name="orchestrator_runs")
    op.drop_index("ix_orchestrator_runs_conversation_id", table_name="orchestrator_runs")
    op.drop_table("orchestrator_runs")
