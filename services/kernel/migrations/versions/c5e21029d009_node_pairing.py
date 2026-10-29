"""Node pairing codes and node platform / slots / preview fields."""

import sqlalchemy as sa
from alembic import op

revision = "c5e21029d009"
down_revision = "b4e21024d008"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("remote_nodes") as batch:
        batch.add_column(sa.Column("platform", sa.String(20), nullable=False, server_default=""))
        batch.add_column(sa.Column("runner_version", sa.String(50), nullable=False, server_default=""))
        batch.add_column(sa.Column("slots", sa.Integer(), nullable=False, server_default="1"))
        batch.add_column(sa.Column("preview_url", sa.String(500), nullable=False, server_default=""))
        batch.add_column(sa.Column("paired_at", sa.DateTime(), nullable=True))
    op.create_table(
        "pairing_codes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("code_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("resource_type", sa.String(30), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("used_at", sa.DateTime(), nullable=True),
        sa.Column("node_id", sa.String(36), sa.ForeignKey("remote_nodes.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )


def downgrade():
    op.drop_table("pairing_codes")
    with op.batch_alter_table("remote_nodes") as batch:
        for column in ("paired_at", "preview_url", "slots", "runner_version", "platform"):
            batch.drop_column(column)
