"""Link durable execution rounds to persistent conversations, preserving legacy sessions."""

from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision = "a7c21010d001"
down_revision = "f41a82d76b90"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("conversations") as batch:
        batch.add_column(sa.Column("agent_id", sa.String(100), nullable=False, server_default="main"))
        batch.add_column(sa.Column("computer_id", sa.String(36)))
        batch.add_column(sa.Column("next_sequence", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("updated_at", sa.DateTime(), nullable=True))
        batch.create_foreign_key("fk_conversation_computer", "computers", ["computer_id"], ["id"])
    with op.batch_alter_table("tasks") as batch:
        batch.add_column(sa.Column("conversation_id", sa.String(36)))
        batch.create_foreign_key("fk_task_conversation", "conversations", ["conversation_id"], ["id"])
        batch.create_index("ix_tasks_conversation_id", ["conversation_id"])
    with op.batch_alter_table("messages") as batch:
        batch.add_column(sa.Column("sequence", sa.Integer()))
        batch.add_column(sa.Column("client_id", sa.String(100)))
        batch.add_column(sa.Column("task_id", sa.String(36)))
        batch.add_column(sa.Column("delivered_at", sa.DateTime()))
        batch.add_column(sa.Column("submission_id", sa.String(200)))
        batch.create_foreign_key("fk_message_task", "tasks", ["task_id"], ["id"])
        batch.create_index("ix_messages_task_id", ["task_id"])
        batch.create_unique_constraint("uq_message_sequence", ["conversation_id", "sequence"])
        batch.create_unique_constraint("uq_message_client", ["conversation_id", "client_id"])

    from niucai.storage.db import Conversation, Event, Message, Task

    connection = op.get_bind()
    conversations, messages, tasks, events = (
        Conversation.__table__,
        Message.__table__,
        Task.__table__,
        Event.__table__,
    )
    for conversation in connection.execute(sa.select(conversations)).mappings().all():
        rows = (
            connection.execute(
                sa.select(messages)
                .where(messages.c.conversation_id == conversation["id"])
                .order_by(messages.c.created_at, messages.c.id)
            )
            .mappings()
            .all()
        )
        for sequence, message in enumerate(rows, 1):
            connection.execute(
                messages.update()
                .where(messages.c.id == message["id"])
                .values(
                    sequence=sequence,
                    status="INTERRUPTED" if message["status"] == "PENDING" else message["status"],
                )
            )
            connection.execute(
                events.insert().values(
                    type="conversation.message_created",
                    created_at=message["created_at"],
                    data={
                        "conversation_id": conversation["id"],
                        "message_id": message["id"],
                        "sequence": sequence,
                    },
                )
            )
        connection.execute(
            conversations.update()
            .where(conversations.c.id == conversation["id"])
            .values(
                next_sequence=len(rows),
                updated_at=rows[-1]["created_at"] if rows else conversation["created_at"],
            )
        )
    for task in connection.execute(sa.select(tasks)).mappings().all():
        cid, mid = str(uuid4()), str(uuid4())
        connection.execute(
            conversations.insert().values(
                id=cid,
                title=task["title"],
                agent_id=task["agent_id"],
                computer_id=task["computer_id"],
                next_sequence=1,
                created_at=task["created_at"],
                updated_at=task["updated_at"],
            )
        )
        connection.execute(tasks.update().where(tasks.c.id == task["id"]).values(conversation_id=cid))
        connection.execute(
            messages.insert().values(
                id=mid,
                conversation_id=cid,
                task_id=task["id"],
                sequence=1,
                role="user",
                content=task["goal"],
                status="COMPLETED",
                delivered_at=task["created_at"],
                created_at=task["created_at"],
            )
        )
        connection.execute(
            events.insert().values(
                type="conversation.message_created",
                task_id=task["id"],
                created_at=task["created_at"],
                data={"conversation_id": cid, "message_id": mid, "sequence": 1},
            )
        )
        result = task["checkpoint"].get("result")
        if result:
            mid = str(uuid4())
            connection.execute(
                messages.insert().values(
                    id=mid,
                    conversation_id=cid,
                    task_id=task["id"],
                    sequence=2,
                    role="assistant",
                    content=str(result),
                    status="COMPLETED",
                    created_at=task["updated_at"],
                )
            )
            connection.execute(
                conversations.update().where(conversations.c.id == cid).values(next_sequence=2)
            )
            connection.execute(
                events.insert().values(
                    type="conversation.message_created",
                    task_id=task["id"],
                    created_at=task["updated_at"],
                    data={"conversation_id": cid, "message_id": mid, "sequence": 2},
                )
            )
    with op.batch_alter_table("conversations") as batch:
        batch.alter_column("updated_at", existing_type=sa.DateTime(), nullable=False)
        batch.alter_column("agent_id", server_default=None)
        batch.alter_column("next_sequence", server_default=None)
    condition = sa.text("status NOT IN ('COMPLETED', 'CANCELLED')")
    op.create_index(
        "uq_conversation_active_task",
        "tasks",
        ["conversation_id"],
        unique=True,
        sqlite_where=condition,
        postgresql_where=condition,
    )


def downgrade():
    # Reverting would discard admission/delivery state. Require a matching database/session backup.
    raise RuntimeError("Restore the pre-migration database and pi_sessions backup to downgrade")
