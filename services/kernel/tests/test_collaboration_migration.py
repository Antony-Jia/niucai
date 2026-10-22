"""Upgrade the already-applied collaboration schema without losing chat history."""

import os
from datetime import datetime, timedelta

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_forward_recovery_of_applied_collaboration_schema(tmp_path, monkeypatch):
    url = os.environ.get("NIUCAI_RECOVERY_TEST_DATABASE_URL", f"sqlite:///{tmp_path}/recovery.db")
    monkeypatch.setenv("NIUCAI_DATABASE_URL", url)
    config = Config("alembic.ini")
    command.upgrade(config, "c9d21011d003")
    engine = sa.create_engine(url)
    stamp = datetime(2026, 10, 11)
    with engine.begin() as connection:
        # Reproduce the original c9 schema that is already installed locally.
        with Operations(MigrationContext.configure(connection)).batch_alter_table("work_items") as batch:
            batch.alter_column("conversation_id", existing_type=sa.String(36), nullable=False)
    metadata = sa.MetaData()
    metadata.reflect(engine)

    def insert(connection, table, **values):
        # Supply frozen schema fields, avoiding current ORM columns added after c9.
        row = {}
        for column in metadata.tables[table].columns:
            if column.name in values or column.nullable or column.server_default is not None:
                continue
            if isinstance(column.type, sa.DateTime):
                row[column.name] = stamp
            elif isinstance(column.type, sa.JSON):
                row[column.name] = {}
            elif isinstance(column.type, sa.Integer):
                row[column.name] = 0
            else:
                row[column.name] = "legacy"
        connection.execute(metadata.tables[table].insert().values(**(row | values)))

    with engine.begin() as connection:
        insert(connection, "agents", id="main")
        for ident in ["ordinary", "dedicated"]:
            insert(connection, "conversations", id=ident)
            insert(
                connection, "tasks", id=ident, agent_id="main", conversation_id=ident, status="WAITING_HUMAN"
            )
            insert(
                connection,
                "workflows",
                id=ident,
                origin_conversation_id=ident,
                owner_agent_id="main",
                status="ACTIVE",
            )
            insert(
                connection,
                "work_items",
                id=ident,
                workflow_id=ident,
                assignee_agent_id="main",
                conversation_id=ident,
                task_id=ident,
            )
            insert(
                connection,
                "messages",
                id=ident,
                task_id=ident,
                conversation_id=ident,
                role="user",
                source="USER",
                workflow_id=None if ident == "ordinary" else ident,
            )
        insert(
            connection,
            "handoffs",
            id="waiting",
            workflow_id="ordinary",
            from_work_item_id="ordinary",
            to_work_item_id="dedicated",
            from_task_id="ordinary",
            to_task_id="dedicated",
            status="ACCEPTED",
        )
        insert(
            connection,
            "task_runs",
            id="false-exit",
            task_id="ordinary",
            status="CANCELLED",
            ended_at=stamp,
            process_exited_at=stamp,
        )
        insert(
            connection,
            "task_runs",
            id="real-exit",
            task_id="ordinary",
            status="COMPLETED",
            ended_at=stamp,
            process_exited_at=stamp + timedelta(seconds=1),
        )
        insert(
            connection,
            "task_runs",
            id="expired",
            task_id="ordinary",
            status="EXPIRED",
            ended_at=stamp,
            process_exited_at=stamp + timedelta(seconds=2),
        )
    command.upgrade(config, "head")
    metadata = sa.MetaData()
    metadata.reflect(engine)
    with engine.connect() as connection:
        items = dict(
            connection.execute(
                sa.select(metadata.tables["work_items"].c.id, metadata.tables["work_items"].c.conversation_id)
            ).all()
        )
        assert items == {"ordinary": None, "dedicated": "dedicated"}
        assert metadata.tables["work_items"].c.conversation_id.nullable
        runs = {
            row["id"]: row for row in connection.execute(sa.select(metadata.tables["task_runs"])).mappings()
        }
        assert runs["false-exit"]["process_exited_at"] is None
        assert runs["expired"]["process_exited_at"] is None
        assert runs["real-exit"]["process_exited_at"] == stamp + timedelta(seconds=1)
        assert all(row["exit_tracking_started_at"] is None for row in runs.values())
        assert (
            connection.execute(sa.select(metadata.tables["handoffs"].c.human_wait_started_at)).scalar_one()
            == stamp
        )
        assert (
            connection.execute(
                sa.select(sa.func.count()).select_from(metadata.tables["messages"])
            ).scalar_one()
            == 2
        )
    command.check(config)
    engine.dispose()
