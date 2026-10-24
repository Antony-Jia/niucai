"""Remote job execution engine (pi / codex / claude)."""

import sqlalchemy as sa
from alembic import op

revision = "b4e21024d008"
down_revision = "a3d21022d007"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("remote_jobs") as batch:
        batch.add_column(sa.Column("engine", sa.String(20), nullable=False, server_default="pi"))


def downgrade():
    with op.batch_alter_table("remote_jobs") as batch:
        batch.drop_column("engine")
