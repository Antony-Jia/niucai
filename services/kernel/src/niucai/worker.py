import asyncio
import logging

from opentelemetry import trace

from niucai.actions.adapters import DisabledAdapter, FakeAdapter, LocalAdapter
from niucai.actions.gateway import ActionGateway
from niucai.agents.selection import RuntimeRouter
from niucai.config import Settings
from niucai.context.compiler import ContextCompiler
from niucai.control.tasks import ComputerRequired, Conflict, TaskManager, require
from niucai.domain.schemas import Decision
from niucai.models.gateway import ModelGateway, ModelRouter
from niucai.storage.db import Action, Database, Task

log = logging.getLogger("niucai.worker")


def make_adapter(settings):
    return {"disabled": DisabledAdapter, "fake": FakeAdapter, "local": lambda: LocalAdapter(settings)}[
        settings.adapter
    ]()


class Worker:
    def __init__(self, db, settings, runtime, adapter):
        self.db, self.settings, self.runtime = db, settings, runtime
        self.tasks = TaskManager(db, settings)
        self.context = ContextCompiler(db, settings)
        self.actions = ActionGateway(db, settings, adapter)

    async def heartbeat(self, task_id, token):
        while True:
            await asyncio.sleep(max(1, self.settings.lease_seconds / 3))
            if not await asyncio.to_thread(self.tasks.heartbeat, task_id, token):
                return

    async def run_claimed(self, task):
        with trace.get_tracer("niucai.worker").start_as_current_span("task.run") as span:
            span.set_attribute("task.id", task.id)
            span.set_attribute("task.run_token", task.run_token)
            return await self._run_claimed(task)

    async def _run_claimed(self, task):
        token = task.run_token
        heartbeat = asyncio.create_task(self.heartbeat(task.id, token))
        try:
            runtime = (
                self.runtime.select(self, task) if isinstance(self.runtime, RuntimeRouter) else self.runtime
            )
            if hasattr(runtime, "run"):
                outcome = await runtime.run(self, task)
                with self.db.sessions() as s:
                    current = require(s, Task, task.id)
                self.tasks.update(
                    task.id,
                    token,
                    status=outcome["status"],
                    checkpoint={**current.checkpoint, **outcome["checkpoint"]},
                )
                return
            for _ in range(self.settings.max_steps + 1):
                with self.db.sessions() as s:
                    task = require(s, Task, task.id)
                    self.actions.check_task(task, token)
                if task.current_step >= task.checkpoint.get("budget_limit", self.settings.max_steps):
                    self.tasks.update(
                        task.id,
                        token,
                        status="WAITING_HUMAN",
                        checkpoint={**task.checkpoint, "reason": "step budget exhausted"},
                    )
                    return
                if not task.plan:
                    plan = await runtime.plan(self.context.compile(task.id), task.id)
                    task = self.tasks.update(task.id, token, plan=plan.model_dump(mode="json"))
                if task.checkpoint.get("proposal"):
                    decision = Decision.model_validate(task.checkpoint["proposal"]).validate_action()
                else:
                    decision = await runtime.decide(self.context.compile(task.id), task.id)
                    task = self.tasks.update(
                        task.id,
                        token,
                        checkpoint={**task.checkpoint, "proposal": decision.model_dump(mode="json")},
                    )
                if decision.kind != "action":
                    self.tasks.update(
                        task.id,
                        token,
                        status="COMPLETED" if decision.kind == "complete" else "WAITING_HUMAN",
                        checkpoint={
                            **{k: v for k, v in task.checkpoint.items() if k != "proposal"},
                            "result": decision.explanation,
                        },
                    )
                    return
                action = self.actions.propose(
                    task.id, token, decision.action.model_dump(mode="json"), f"{task.id}:{task.current_step}"
                )
                action = await self.actions.execute(action.id, token)
                if action.status in {"WAITING_APPROVAL", "UNKNOWN"}:
                    self.tasks.update(
                        task.id,
                        token,
                        status="WAITING_HUMAN",
                        checkpoint={**task.checkpoint, "action_id": action.id, "reason": action.status},
                    )
                    return
                self.tasks.update(
                    task.id,
                    token,
                    current_step=task.current_step + 1,
                    checkpoint={
                        **{k: v for k, v in task.checkpoint.items() if k != "proposal"},
                        "budget_limit": task.checkpoint.get("budget_limit", self.settings.max_steps),
                        "last_action_id": action.id,
                        "last_result": action.result,
                        "last_action_status": action.status,
                    },
                )
        except ComputerRequired as exc:
            try:
                with self.db.sessions() as s:
                    current = require(s, Task, task.id)
                self.tasks.update(
                    task.id,
                    token,
                    status="WAITING_HUMAN",
                    checkpoint={**current.checkpoint, "reason": "computer required", "detail": str(exc)},
                )
            except Conflict:
                pass
        except Conflict:
            log.info("task %s lease/control changed", task.id)
        except Exception as exc:
            log.exception("task %s failed", task.id)
            try:
                with self.db.sessions() as s:
                    current = require(s, Task, task.id)
                self.tasks.update(
                    task.id,
                    token,
                    status="FAILED",
                    checkpoint={**current.checkpoint, "error": type(exc).__name__, "detail": str(exc)[:500]},
                )
            except Conflict:
                pass
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass

    async def run_once(self):
        task = self.tasks.claim()
        if task is None:
            return False
        await self.run_claimed(task)
        return True

    def resolve_unknown(self, action_id, outcome):
        # Administrative resolution is deliberately separate from approval.
        with self.db.sessions.begin() as s:
            action = require(s, Action, action_id)
            require(s, Task, action.task_id, lock=True)
            action = require(s, Action, action_id, lock=True)
            if action.status != "UNKNOWN" or outcome not in {"SUCCEEDED", "FAILED"}:
                raise Conflict("only UNKNOWN actions can be reconciled")
            from niucai.storage.db import Audit, emit

            action.status = outcome
            action.result = {**action.result, "human_reconciled": True}
            s.add(Audit(action_id=action.id, outcome=outcome, data={"human_reconciled": True}))
            emit(s, "action.reconciled", action.task_id, action_id=action.id, outcome=outcome)
            return action


async def main():
    from niucai.observability import configure

    configure()
    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    if settings.lease_seconds <= settings.action_timeout * 2:
        raise ValueError("lease_seconds must exceed twice action_timeout")
    db = Database(settings.database_url)
    gateway = ModelGateway(db, settings, ModelRouter(settings.model_config_path))
    runtime = RuntimeRouter(gateway)
    adapter = make_adapter(settings)
    worker = Worker(db, settings, runtime, adapter)
    try:
        while True:
            if not await worker.run_once():
                await asyncio.sleep(settings.poll_seconds)
    finally:
        await gateway.close()
        if hasattr(adapter, "close"):
            await adapter.close()
        db.engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
