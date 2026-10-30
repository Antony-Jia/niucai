"""Bot 增加推理强度 reasoning_effort

Revision ID: fb021029d015
Revises: fa021029d014
"""

import sqlalchemy as sa
from alembic import op

revision = "fb021029d015"
down_revision = "fa021029d014"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("bots", sa.Column("reasoning_effort", sa.String(20), nullable=False, server_default=""))


def downgrade():
    op.drop_column("bots", "reasoning_effort")
