"""Bot 协作 C2：协作策略、卡片等待条件与委派深度

Revision ID: fd021029d017
Revises: fc021029d016
"""

import sqlalchemy as sa
from alembic import op

revision = "fd021029d017"
down_revision = "fc021029d016"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("bots", sa.Column("collab_policy", sa.JSON(), nullable=False, server_default="{}"))
    op.add_column("cards", sa.Column("waiting_on", sa.JSON(), nullable=True))
    op.add_column("cards", sa.Column("depth", sa.Integer(), nullable=False, server_default="0"))


def downgrade():
    with op.batch_alter_table("cards") as batch:
        batch.drop_column("depth")
        batch.drop_column("waiting_on")
    with op.batch_alter_table("bots") as batch:
        batch.drop_column("collab_policy")
