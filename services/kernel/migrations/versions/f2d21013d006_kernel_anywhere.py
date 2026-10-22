"""Kernel identity, work configuration snapshots and bounded artifact transport."""

import sqlalchemy as sa
from alembic import op

revision = "f2d21013d006"
down_revision = "e1d21011d005"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("remote_jobs") as batch:
        batch.add_column(sa.Column("wait_state", sa.JSON(), nullable=False, server_default="{}"))
    op.create_table(
        "kernel_instance",
        sa.Column("id", sa.String(20), primary_key=True),
        sa.Column("instance_id", sa.String(36), nullable=False),
        sa.Column("admission_enabled", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "worker_health",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("heartbeat_at", sa.DateTime(), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
    )
    for table, column in [
        ("work_items", "definition_snapshot"),
        ("remote_jobs", "config_snapshot"),
        ("remote_nodes", "connection"),
    ]:
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column(column, sa.JSON(), nullable=False, server_default="{}"))
    op.create_table(
        "artifact_uploads",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("job_id", sa.String(36), sa.ForeignKey("remote_jobs.id"), nullable=False),
        sa.Column("attempt_id", sa.String(36), nullable=False),
        sa.Column("filename", sa.String(200), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("media_type", sa.String(100), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("received", sa.Integer(), nullable=False),
        sa.Column("artifact_id", sa.String(36), sa.ForeignKey("artifacts.id")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("job_id", "attempt_id", "filename"),
    )
    op.create_index("ix_artifact_uploads_job_id", "artifact_uploads", ["job_id"])


def downgrade():
    raise RuntimeError(
        "Restore a backup with the previous software; this revision has no destructive downgrade"
    )
