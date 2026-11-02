"""Bot 协作 C3：裁判运行、Goal 级裁判开关与 decision 记忆的结构化字段

Revision ID: fe021029d018
Revises: fd021029d017
"""

import sqlalchemy as sa
from alembic import op

revision = "fe021029d018"
down_revision = "fd021029d017"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("conversations", sa.Column("judge_enabled", sa.Boolean(), nullable=True))
    op.add_column("memories", sa.Column("meta", sa.JSON(), nullable=True))
    op.create_table(
        "judge_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("card_id", sa.String(36), sa.ForeignKey("cards.id"), nullable=False),
        sa.Column("conversation_id", sa.String(36), nullable=True),
        sa.Column("question_event_id", sa.Integer(), sa.ForeignKey("card_events.id"), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("verdict", sa.String(10), nullable=True),
        sa.Column("record_ids", sa.JSON(), nullable=False),
        sa.Column("cited_ids", sa.JSON(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("usage", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("lease_until", sa.DateTime(), nullable=True),
        sa.Column("correction", sa.Text(), nullable=True),
        sa.Column("correction_record_id", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("question_event_id"),
    )
    op.create_index("ix_judge_runs_card_id", "judge_runs", ["card_id"])
    op.create_index("ix_judge_runs_conversation_id", "judge_runs", ["conversation_id"])
    op.create_index("ix_judge_runs_status", "judge_runs", ["status"])


def downgrade():
    op.drop_index("ix_judge_runs_status", table_name="judge_runs")
    op.drop_index("ix_judge_runs_conversation_id", table_name="judge_runs")
    op.drop_index("ix_judge_runs_card_id", table_name="judge_runs")
    op.drop_table("judge_runs")
    with op.batch_alter_table("memories") as batch:
        batch.drop_column("meta")
    with op.batch_alter_table("conversations") as batch:
        batch.drop_column("judge_enabled")
