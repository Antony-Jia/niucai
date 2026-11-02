"""Bot 协作 C1：环境画像、资源声明、事件回复关系、产物存储

Revision ID: fc021029d016
Revises: fb021029d015
"""

import sqlalchemy as sa
from alembic import op

revision = "fc021029d016"
down_revision = "fb021029d015"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("bots", sa.Column("environment", sa.JSON(), nullable=False, server_default="{}"))
    op.add_column("cards", sa.Column("claims", sa.JSON(), nullable=False, server_default="[]"))
    with op.batch_alter_table("card_events") as batch:
        batch.add_column(sa.Column("reply_to", sa.Integer(), nullable=True))
        batch.create_foreign_key("fk_card_events_reply_to", "card_events", ["reply_to"], ["id"])
    op.create_table(
        "board_artifacts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("conversation_id", sa.String(36), sa.ForeignKey("conversations.id"), nullable=True),
        sa.Column("card_id", sa.String(36), sa.ForeignKey("cards.id"), nullable=False),
        sa.Column("bot_id", sa.String(36), nullable=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_board_artifacts_conversation_id", "board_artifacts", ["conversation_id"])
    op.create_index("ix_board_artifacts_card_id", "board_artifacts", ["card_id"])


def downgrade():
    op.drop_table("board_artifacts")
    with op.batch_alter_table("card_events") as batch:
        batch.drop_constraint("fk_card_events_reply_to", type_="foreignkey")
        batch.drop_column("reply_to")
    with op.batch_alter_table("cards") as batch:
        batch.drop_column("claims")
    with op.batch_alter_table("bots") as batch:
        batch.drop_column("environment")
