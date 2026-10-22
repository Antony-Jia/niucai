import asyncio
import logging
import time

from opentelemetry import trace

from niucai.actions.adapters import DisabledAdapter, FakeAdapter, LocalAdapter
from niucai.actions.gateway import ActionGateway
from niucai.agents.selection import RuntimeRouter
from niucai.config import Settings
from niucai.context.compiler import ContextCompiler
from niucai.control import report
from niucai.control.progress import run_context
from niucai.control.tasks import ComputerBusy, ComputerRequired, Conflict, TaskManager, require
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
        if db.engine.dialect.name == "sqlite" and settings.worker_slots > 1:
            raise ValueError("SQLite supports NIUCAI_WORKER_SLOTS=1 only")
        if not settings.multi_bot_enabled:
            from sqlalchemy import inspect, select

            from niucai.storage.db import Workflow

            if inspect(db.engine).has_table("workflows"):
                with db.sessions() as session:
                    if session.scalar(select(Workflow.id).where(Workflow.status == "ACTIVE").limit(1)):
                        raise ValueError(
                            "pause or stop active workflows before disabling multi Bot collaboration"
                        )
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
            execution = asyncio.create_task(self._run_claimed(task))
            monitor = asyncio.create_task(self.watch_run(task.id, task.run_token))
            try:
                done, _ = await asyncio.wait({execution, monitor}, return_when=asyncio.FIRST_COMPLETED)
                if execution in done:
                    return await execution
                reason = await monitor
                execution.cancel()
                await asyncio.gather(execution, return_exceptions=True)
                if reason == "idle":
                    try:
                        with self.db.sessions() as s:
                            current = require(s, Task, task.id)
                        await asyncio.to_thread(
                            self.tasks.update,
                            task.id,
                            task.run_token,
                            status="FAILED",
                            checkpoint={
                                **current.checkpoint,
                                "error": "RuntimeIdleTimeout",
                                "detail": "运行时长时间没有业务进展，已终止本轮执行；请检查后重试。",
                            },
                        )
                    except Conflict:
                        pass  # User control or UNKNOWN handling already settled this run.
            finally:
                execution.cancel()
                monitor.cancel()
                await asyncio.gather(execution, monitor, return_exceptions=True)

                def confirm_exit():
                    from niucai.storage.db import TaskRun, now

                    with self.db.sessions.begin() as session:
                        run = session.get(TaskRun, task.run_token)
                        if run:
                            run.exit_tracking_started_at = run.exit_tracking_started_at or now()
                            run.process_exited_at = now()

                await asyncio.to_thread(confirm_exit)

    async def watch_run(self, task_id, token):
        marker, changed_at = None, time.monotonic()

        def snapshot():
            with self.db.sessions() as s:
                task = require(s, Task, task_id)
                self.actions.check_task(task, token)
                return task.checkpoint.get("_progress", {}).get("last_progress_at")

        while True:
            try:
                current = await asyncio.to_thread(snapshot)
            except Conflict:
                return "revoked"
            if current != marker:
                marker, changed_at = current, time.monotonic()
            if time.monotonic() - changed_at >= self.settings.runtime_idle_timeout:
                return "idle"
            await asyncio.sleep(min(0.25, self.settings.runtime_idle_timeout / 4))

    async def _run_claimed(self, task):
        token = task.run_token
        context_token = run_context.set((task.id, token))
        heartbeat = asyncio.create_task(self.heartbeat(task.id, token))
        runtime = None
        try:
            runtime = (
                self.runtime.select(self, task) if isinstance(self.runtime, RuntimeRouter) else self.runtime
            )
            if hasattr(runtime, "run"):
                outcome = await runtime.run(self, task)
                try:
                    outcome = await report.build(self, runtime, task, outcome)
                except Exception:  # 收尾报告是附加能力，失败时保留原结果。
                    log.exception("task %s final report failed", task.id)
                with self.db.sessions() as s:
                    current = require(s, Task, task.id)
                await asyncio.to_thread(
                    self.tasks.update,
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
                    await asyncio.to_thread(
                        self.tasks.update,
                        task.id,
                        token,
                        status="WAITING_HUMAN",
                        checkpoint={**task.checkpoint, "reason": "step budget exhausted"},
                    )
                    return
                if not task.plan:
                    await asyncio.to_thread(self.tasks.phase, task.id, token, "PLANNING")
                    plan = await runtime.plan(self.context.compile(task.id), task.id)
                    task = await asyncio.to_thread(
                        self.tasks.update, task.id, token, plan=plan.model_dump(mode="json")
                    )
                if task.checkpoint.get("proposal"):
                    decision = Decision.model_validate(task.checkpoint["proposal"]).validate_action()
                else:
                    await asyncio.to_thread(self.tasks.phase, task.id, token, "MODEL_REQUEST")
                    decision = await runtime.decide(self.context.compile(task.id), task.id)
                    task = await asyncio.to_thread(
                        self.tasks.update,
                        task.id,
                        token,
                        checkpoint={**task.checkpoint, "proposal": decision.model_dump(mode="json")},
                    )
                if decision.kind != "action":
                    await asyncio.to_thread(
                        self.tasks.update,
                        task.id,
                        token,
                        status="COMPLETED" if decision.kind == "complete" else "WAITING_HUMAN",
                        checkpoint={
                            **{k: v for k, v in task.checkpoint.items() if k != "proposal"},
                            "result": decision.explanation,
                            **({"reason": "agent requested human"} if decision.kind == "wait" else {}),
                        },
                    )
                    return
                action = await asyncio.to_thread(
                    self.actions.propose,
                    task.id,
                    token,
                    decision.action.model_dump(mode="json"),
                    f"{task.id}:{task.current_step}",
                )
                action = await self.actions.execute(action.id, token)
                if action.status in {"WAITING_APPROVAL", "UNKNOWN"}:
                    await asyncio.to_thread(
                        self.tasks.update,
                        task.id,
                        token,
                        status="WAITING_HUMAN",
                        checkpoint={**task.checkpoint, "action_id": action.id, "reason": action.status},
                    )
                    return
                await asyncio.to_thread(
                    self.tasks.update,
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
                await asyncio.to_thread(
                    self.tasks.update,
                    task.id,
                    token,
                    status="PENDING"
                    if self.settings.multi_bot_enabled and isinstance(exc, ComputerBusy)
                    else "WAITING_HUMAN",
                    checkpoint={
                        **current.checkpoint,
                        "reason": "computer_busy"
                        if self.settings.multi_bot_enabled and isinstance(exc, ComputerBusy)
                        else "computer required",
                        "detail": str(exc),
                    },
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
                failed = {
                    "status": "FAILED",
                    "checkpoint": {
                        **current.checkpoint,
                        "error": type(exc).__name__,
                        "detail": str(exc)[:500],
                    },
                }
                try:
                    failed = await report.build(self, runtime, task, failed)
                except Exception:
                    log.exception("task %s failure report failed", task.id)
                await asyncio.to_thread(
                    self.tasks.update, task.id, token, status="FAILED", checkpoint=failed["checkpoint"]
                )
            except Conflict:
                pass
        finally:
            run_context.reset(context_token)
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass

    async def run_once(self):
        task = await asyncio.to_thread(self.tasks.claim)
        if task is None:
            return False
        await self.run_claimed(task)
        return True

    async def serve(self):
        from niucai.control.collaboration import CollaborationManager

        async def slot():
            while True:
                if not await self.run_once():
                    await asyncio.sleep(self.settings.poll_seconds)

        async def scheduler():
            collaboration = CollaborationManager(self.db, self.settings)
            from niucai.control.instance import worker_heartbeat
            from niucai.storage.db import uid

            worker_id = uid()
            cleanup_tick = 0
            while True:
                await asyncio.to_thread(worker_heartbeat, self.db, self.settings, worker_id)
                cleanup_tick += 1
                if cleanup_tick % 60 == 0:
                    from niucai.control.artifact_transport import ArtifactTransport

                    await asyncio.to_thread(ArtifactTransport(self.db, self.settings).cleanup)
                await asyncio.to_thread(collaboration.tick)
                if self.settings.multi_bot_enabled and self.settings.routines_enabled:
                    from niucai.control.routines import fire_due

                    await asyncio.to_thread(fire_due, self.db, self.settings)
                await asyncio.sleep(self.settings.poll_seconds)

        runners = [asyncio.create_task(slot()) for _ in range(self.settings.worker_slots)]
        runners.append(asyncio.create_task(scheduler()))
        try:
            await asyncio.gather(*runners)
        finally:
            for runner in runners:
                runner.cancel()
            await asyncio.gather(*runners, return_exceptions=True)

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
        await worker.serve()
    finally:
        await gateway.close()
        if hasattr(adapter, "close"):
            await adapter.close()
        db.engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
