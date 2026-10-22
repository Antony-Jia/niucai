"""Real PostgreSQL stop-gate sharing and writer exclusion."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from niucai.control.collaboration import gate


def test_postgres_shared_gate_allows_readers_but_blocks_stop(setup):
    db = setup[0]
    if db.engine.dialect.name != "postgresql":
        pytest.skip("requires dedicated PostgreSQL test database")
    with db.sessions.begin() as session:
        gate(session)
    ready = Barrier(3)
    release = Event()

    def reader():
        with db.sessions.begin() as session:
            assert not gate(session, shared=True).stopped
            ready.wait(timeout=5)
            assert release.wait(timeout=5)

    with ThreadPoolExecutor(max_workers=2) as pool:
        readers = [pool.submit(reader) for _ in range(2)]
        try:
            # Both readers must reach the barrier while their transactions are open.
            ready.wait(timeout=5)
            with pytest.raises(OperationalError) as error:
                with db.sessions.begin() as session:
                    session.execute(text("SET LOCAL lock_timeout = '200ms'"))
                    gate(session).stopped = 1
            assert error.value.orig.sqlstate == "55P03"
        finally:
            release.set()
        for future in readers:
            future.result(timeout=5)
    with db.sessions.begin() as session:
        gate(session).stopped = 1
    with db.sessions() as session:
        assert gate(session, shared=True).stopped
