"""Conversation settings (mode/YOLO), skill library and MCP server registry."""

import sqlalchemy as sa
from alembic import op

revision = "a3d21022d007"
down_revision = "f2d21013d006"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("conversations") as batch:
        batch.add_column(sa.Column("settings", sa.JSON(), nullable=False, server_default="{}"))
    op.create_table(
        "skills",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False, unique=True),
        sa.Column("description", sa.String(1000), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "mcp_servers",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False, unique=True),
        sa.Column("transport", sa.String(20), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("tools", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Integer(), nullable=False),
        sa.Column("auto_approve_read_only", sa.Integer(), nullable=False),
        sa.Column("status", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )


def downgrade():
    op.drop_table("mcp_servers")
    op.drop_table("skills")
    with op.batch_alter_table("conversations") as batch:
        batch.drop_column("settings")
