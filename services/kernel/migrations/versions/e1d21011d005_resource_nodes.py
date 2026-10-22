"""Classify execution resources without replacing existing node identities."""

import sqlalchemy as sa
from alembic import op

revision = "e1d21011d005"
down_revision = "d0d21011d004"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("remote_nodes") as batch:
        batch.add_column(
            sa.Column("resource_type", sa.String(30), nullable=False, server_default="unspecified")
        )


def downgrade():
    with op.batch_alter_table("remote_nodes") as batch:
        batch.drop_column("resource_type")
