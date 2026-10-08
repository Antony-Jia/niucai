import asyncio

import httpx
import pytest
from sqlalchemy import select

from niucai.actions.adapters import FakeAdapter
from niucai.domain.schemas import Role, TaskCreate
from niucai.models.gateway import ModelGateway, ModelRouter
from niucai.storage.db import Event, Task
from niucai.worker import Worker


class HangingRuntime:
    def __init__(self):
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()

    async def run(self, worker, task):
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.stopped.set()


@pytest.mark.parametrize("operation", ["pause", "cancel"])
async def test_control_interrupts_waiting_runtime_and_releases_worker(setup, operation):
    db, settings, manager, _ = setup
    task = manager.create(TaskCreate(title="waiting", goal="test"))
    runtime = HangingRuntime()
    worker = Worker(db, settings, runtime, FakeAdapter())
    running = asyncio.create_task(worker.run_once())
    await asyncio.wait_for(runtime.started.wait(), 2)
    manager.transition(task.id, operation)
    await asyncio.wait_for(running, 2)
    assert runtime.stopped.is_set()
    with db.sessions() as s:
        current = s.get(Task, task.id)
        assert current.status == ("PAUSED" if operation == "pause" else "CANCELLED")
        assert current.run_token is None


async def test_idle_runtime_fails_and_worker_can_claim_next_task(setup):
    db, settings, manager, _ = setup
    settings.runtime_idle_timeout = 0.08
    first = manager.create(TaskCreate(title="stuck", goal="test"))
    second = manager.create(TaskCreate(title="next", goal="test"))
    runtime = HangingRuntime()
    worker = Worker(db, settings, runtime, FakeAdapter())
    await asyncio.wait_for(worker.run_once(), 2)
    assert runtime.stopped.is_set()
    with db.sessions() as s:
        task = s.get(Task, first.id)
        assert task.status == "FAILED"
        assert task.checkpoint["error"] == "RuntimeIdleTimeout"
    assert manager.claim().id == second.id


async def test_business_progress_resets_idle_limit(setup):
    db, settings, manager, _ = setup
    settings.runtime_idle_timeout = 0.12
    task = manager.create(TaskCreate(title="progress", goal="test"))

    class AdvancingRuntime:
        async def run(self, worker, task):
            for _ in range(5):
                worker.tasks.phase(task.id, task.run_token, "PROCESSING")
                await asyncio.sleep(0.04)
            return {"status": "COMPLETED", "checkpoint": {"result": "done"}}

    await Worker(db, settings, AdvancingRuntime(), FakeAdapter()).run_once()
    with db.sessions() as s:
        assert s.get(Task, task.id).status == "COMPLETED"


async def test_model_total_deadline_cancels_request(setup, tmp_path):
    db, settings, _, _ = setup
    settings.model_timeout = 0.04
    settings.openai_api_key = "test-only"
    config = tmp_path / "models.yaml"
    config.write_text('roles:\n  executor: {model: "test/model"}\n')
    stopped = asyncio.Event()

    async def handler(request):
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    gateway = ModelGateway(
        db, settings, ModelRouter(config), httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    try:
        with pytest.raises(TimeoutError, match="模型响应超时"):
            await gateway.chat(Role.EXECUTOR, [], "timeout-test")
        assert stopped.is_set()
        with db.sessions() as s:
            failures = s.scalars(select(Event).where(Event.type == "model.failed")).all()
            assert len(failures) == 1
            assert failures[0].data["error"] == "ModelTimeout"
    finally:
        await gateway.close()
