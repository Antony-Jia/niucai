"""Exercise real Alembic upgrade against a database with pre-unification history."""

import os
from datetime import datetime

import sqlalchemy as sa
from alembic import command
from alembic.config import Config


def test_upgrade_preserves_legacy_tasks_actions_and_interrupted_chat(tmp_path, monkeypatch):
    url = os.environ.get("NIUCAI_MIGRATION_TEST_DATABASE_URL", f"sqlite:///{tmp_path}/migration.db")
    monkeypatch.setenv("NIUCAI_DATABASE_URL", url)
    config = Config("alembic.ini")
    command.upgrade(config, "f41a82d76b90")
    engine = sa.create_engine(url)
    metadata = sa.MetaData()
    metadata.reflect(engine)
    stamp = datetime(2026, 10, 10)
    with engine.begin() as connection:
        connection.execute(metadata.tables["agents"].insert().values(id="main", name="main", definition={}))
        connection.execute(
            metadata.tables["conversations"].insert().values(id="chat", title="old chat", created_at=stamp)
        )
        connection.execute(
            metadata.tables["messages"]
            .insert()
            .values(
                id="pending",
                conversation_id="chat",
                role="assistant",
                content="",
                status="PENDING",
                created_at=stamp,
            )
        )
        connection.execute(
            metadata.tables["tasks"]
            .insert()
            .values(
                id="legacy",
                title="legacy",
                goal="old goal",
                status="WAITING_HUMAN",
                agent_id="main",
                plan={},
                checkpoint={"pi_submission_id": "persisted", "runtime": "pi"},
                current_step=2,
                retry_count=0,
                created_at=stamp,
                updated_at=stamp,
            )
        )
        connection.execute(
            metadata.tables["actions"]
            .insert()
            .values(
                id="action",
                task_id="legacy",
                agent_id="main",
                idempotency_key="old",
                spec={},
                risk="HIGH",
                status="WAITING_APPROVAL",
                result={},
                created_at=stamp,
            )
        )
        connection.execute(
            metadata.tables["approvals"]
            .insert()
            .values(id="approval", action_id="action", status="PENDING", note="")
        )
    command.upgrade(config, "head")
    metadata = sa.MetaData()
    metadata.reflect(engine)
    with engine.connect() as connection:
        task = connection.execute(sa.select(metadata.tables["tasks"])).mappings().one()
        assert task["id"] == "legacy" and task["current_step"] == 2
        assert task["checkpoint"] == {"pi_submission_id": "persisted", "runtime": "pi"}
        assert task["conversation_id"] != "chat"
        message = (
            connection.execute(
                sa.select(metadata.tables["messages"]).where(metadata.tables["messages"].c.id == "pending")
            )
            .mappings()
            .one()
        )
        assert message["status"] == "INTERRUPTED" and message["sequence"] == 1
        assert connection.execute(sa.select(metadata.tables["approvals"].c.status)).scalar_one() == "PENDING"
        if engine.dialect.name == "sqlite":
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    command.check(config)
    engine.dispose()
