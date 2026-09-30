"""durable browser sessions"""

import sqlalchemy as sa
from alembic import op

revision = "c67565bea646"
down_revision = "e9f876f10403"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "browser_sessions",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("key_fingerprint", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_browser_sessions_expires_at", "browser_sessions", ["expires_at"])


def downgrade():
    op.drop_index("ix_browser_sessions_expires_at", table_name="browser_sessions")
    op.drop_table("browser_sessions")
