"""软删除：conversations / tasks 增加 deleted_at

Revision ID: fa021029d014
Revises: f9a21029d013
"""

import sqlalchemy as sa
from alembic import op

revision = "fa021029d014"
down_revision = "f9a21029d013"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("conversations", "tasks"):
        op.add_column(table, sa.Column("deleted_at", sa.DateTime(), nullable=True))
        op.create_index(f"ix_{table}_deleted_at", table, ["deleted_at"])


def downgrade():
    for table in ("tasks", "conversations"):
        op.drop_index(f"ix_{table}_deleted_at", table_name=table)
        op.drop_column(table, "deleted_at")
