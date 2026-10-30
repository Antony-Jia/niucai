"""goal_requests：Goal 创建幂等键（评审修复）

Revision ID: f9a21029d013
Revises: f8a21029d012
"""

import sqlalchemy as sa
from alembic import op

revision = "f9a21029d013"
down_revision = "f8a21029d012"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "goal_requests",
        sa.Column("client_id", sa.String(100), primary_key=True),
        sa.Column(
            "conversation_id", sa.String(36), sa.ForeignKey("conversations.id"), nullable=False, unique=True
        ),
        sa.Column("content_digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )


def downgrade():
    op.drop_table("goal_requests")
