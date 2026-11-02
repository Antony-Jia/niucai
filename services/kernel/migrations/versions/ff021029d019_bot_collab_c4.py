"""Bot 协作 C4：交谈（ask 卡）——卡片种类与 Attempt 用途

Revision ID: ff021029d019
Revises: fe021029d018
"""

import sqlalchemy as sa
from alembic import op

revision = "ff021029d019"
down_revision = "fe021029d018"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("cards", sa.Column("kind", sa.String(10), nullable=False, server_default="task"))
    op.add_column("remote_jobs", sa.Column("purpose", sa.String(10), nullable=False, server_default="work"))


def downgrade():
    with op.batch_alter_table("remote_jobs") as batch:
        batch.drop_column("purpose")
    with op.batch_alter_table("cards") as batch:
        batch.drop_column("kind")
