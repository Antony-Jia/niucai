"""cards.workdir：卡片指定节点本地目录（重构 P4）

Revision ID: f8a21029d012
Revises: e7a21029d011
"""

import sqlalchemy as sa
from alembic import op

revision = "f8a21029d012"
down_revision = "e7a21029d011"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("cards", sa.Column("workdir", sa.String(length=500), nullable=False, server_default=""))


def downgrade():
    with op.batch_alter_table("cards") as batch:
        batch.drop_column("workdir")
