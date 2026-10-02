import os

import pytest

from niucai.config import Settings
from niucai.control.tasks import TaskManager
from niucai.storage.db import Agent, Computer, Database


@pytest.fixture
def setup(tmp_path):
    url = os.environ.get("NIUCAI_TEST_DATABASE_URL", f"sqlite:///{tmp_path}/test.db")
    settings = Settings(
        database_url=url,
        api_token="x" * 40,
        workspace=tmp_path / "workspace",
        pi_storage=tmp_path / "pi",
        model_config_path="models.yaml",
        poll_seconds=0.01,
    )
    db = Database(url)
    db.create_schema()
    with db.sessions.begin() as s:
        from niucai.storage.db import Base

        for table in reversed(Base.metadata.sorted_tables):
            s.execute(table.delete())
        s.add(Agent(id="main", name="Main", definition={}))
        computer = Computer(name="test", status="ONLINE")
        s.add(computer)
        s.flush()
        ident = computer.id
    yield db, settings, TaskManager(db, settings), ident
    db.engine.dispose()
