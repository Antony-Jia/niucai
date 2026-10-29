"""Bots, whiteboard cards and card timeline; remote_jobs become card attempts."""

import sqlalchemy as sa
from alembic import op

revision = "d6e21029d010"
down_revision = "c5e21029d009"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "bots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(60), nullable=False, unique=True),
        sa.Column("node_id", sa.String(36), sa.ForeignKey("remote_nodes.id"), nullable=False),
        sa.Column("engine", sa.String(20), nullable=False),
        sa.Column("role", sa.String(500), nullable=False),
        sa.Column("instructions", sa.Text(), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("permission_profile", sa.String(20), nullable=False),
        sa.Column("skills", sa.JSON(), nullable=False),
        sa.Column("mcp_servers", sa.JSON(), nullable=False),
        sa.Column("requires_approval", sa.Integer(), nullable=False),
        sa.Column("concurrency", sa.Integer(), nullable=False),
        sa.Column("config_version", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_bots_node_id", "bots", ["node_id"])
    op.create_table(
        "cards",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("conversation_id", sa.String(36), sa.ForeignKey("conversations.id"), nullable=True),
        sa.Column("parent_card_id", sa.String(36), sa.ForeignKey("cards.id"), nullable=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("acceptance", sa.Text(), nullable=False),
        sa.Column("assignee_bot_id", sa.String(36), sa.ForeignKey("bots.id"), nullable=True),
        sa.Column("required_tags", sa.JSON(), nullable=False),
        sa.Column("depends_on", sa.JSON(), nullable=False),
        sa.Column("inputs", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("review", sa.String(10), nullable=False),
        sa.Column("created_by", sa.String(60), nullable=False),
        sa.Column("current_job_id", sa.String(36), nullable=True),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("budget", sa.JSON(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_cards_conversation_id", "cards", ["conversation_id"])
    op.create_index("ix_cards_assignee_bot_id", "cards", ["assignee_bot_id"])
    op.create_index("ix_cards_status", "cards", ["status"])
    op.create_table(
        "card_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("card_id", sa.String(36), sa.ForeignKey("cards.id"), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("actor", sa.String(60), nullable=False),
        sa.Column("mentions", sa.JSON(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=True, unique=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("card_id", "seq"),
    )
    op.create_index("ix_card_events_card_id", "card_events", ["card_id"])
    with op.batch_alter_table("remote_jobs") as batch:
        batch.add_column(sa.Column("card_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("bot_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("board_token_hash", sa.String(64), nullable=True))
        batch.create_foreign_key("fk_remote_jobs_card_id", "cards", ["card_id"], ["id"])
        batch.create_foreign_key("fk_remote_jobs_bot_id", "bots", ["bot_id"], ["id"])
        batch.create_index("ix_remote_jobs_card_id", ["card_id"])
        batch.create_index("ix_remote_jobs_bot_id", ["bot_id"])
        batch.create_index("ix_remote_jobs_board_token_hash", ["board_token_hash"])


def downgrade():
    with op.batch_alter_table("remote_jobs") as batch:
        batch.drop_index("ix_remote_jobs_board_token_hash")
        batch.drop_index("ix_remote_jobs_bot_id")
        batch.drop_index("ix_remote_jobs_card_id")
        batch.drop_constraint("fk_remote_jobs_bot_id", type_="foreignkey")
        batch.drop_constraint("fk_remote_jobs_card_id", type_="foreignkey")
        for column in ("board_token_hash", "bot_id", "card_id"):
            batch.drop_column(column)
    op.drop_table("card_events")
    op.drop_table("cards")
    op.drop_table("bots")
