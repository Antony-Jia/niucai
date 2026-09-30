from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import select

from niucai.domain.schemas import TaskCreate
from niucai.storage.db import Computer, Task, now


def test_postgres_concurrent_claim_same_computer(setup):
    db, settings, manager, computer_id = setup
    if db.engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL row locks required")
    for i in range(2):
        manager.create(TaskCreate(title=str(i), goal="test", computer_id=computer_id))
    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed = list(pool.map(lambda _: manager.claim(), range(2)))
    assert len([t for t in claimed if t]) == 1
    with db.sessions() as s:
        assert len(s.scalars(select(Task).where(Task.status == "RUNNING")).all()) == 1


def test_postgres_skip_locked_row(setup):
    db, settings, manager, _ = setup
    if db.engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL row locks required")
    first = manager.create(TaskCreate(title="first", goal="test"))
    second = manager.create(TaskCreate(title="second", goal="test"))
    with db.sessions.begin() as s:
        s.scalar(select(Task).where(Task.id == first.id).with_for_update())
        with ThreadPoolExecutor(max_workers=1) as pool:
            claimed = pool.submit(manager.claim).result(timeout=5)
        assert claimed.id == second.id


def test_postgres_two_computers_can_run(setup):
    db, settings, manager, computer_id = setup
    if db.engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL row locks required")
    with db.sessions.begin() as s:
        other = Computer(name="other")
        s.add(other)
        s.flush()
        other_id = other.id
    for i, cid in enumerate([computer_id, other_id]):
        manager.create(TaskCreate(title=str(i), goal="test", computer_id=cid))
    first = manager.claim()
    second = manager.claim()
    assert first and second and first.computer_id != second.computer_id
    with db.sessions.begin() as s:
        s.get(Task, first.id).lease_until = now() - timedelta(seconds=1)
    recovered = manager.claim()
    assert recovered.id == first.id and recovered.run_token != first.run_token
