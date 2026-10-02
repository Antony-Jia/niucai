"""Pin the harness per task; existing checkpoints never silently switch formats."""

from sqlalchemy import select

from niucai.agents.runtime import StructuredRuntime
from niucai.storage.db import HarnessCheckpoint


class RuntimeRouter:
    def __init__(self, gateway):
        self.gateway = gateway

    def select(self, worker, task):
        name = task.checkpoint.get("runtime")
        if not name:
            with worker.db.sessions() as session:
                legacy = session.scalar(
                    select(HarnessCheckpoint.thread_id).where(HarnessCheckpoint.thread_id == task.id).limit(1)
                )
            if legacy:
                name = "deepagents"
            elif task.checkpoint.get("proposal") or task.current_step > 0:
                name = "structured"
            else:
                name = worker.settings.runtime
            worker.tasks.update(task.id, task.run_token, checkpoint={**task.checkpoint, "runtime": name})
        if name == "pi":
            from niucai.agents.pi_adapter import PiDurableRuntime

            return PiDurableRuntime(self.gateway)
        if name == "deepagents":
            from niucai.agents.deepagent_adapter import DeepAgentRuntime

            return DeepAgentRuntime(self.gateway)
        if name == "structured":
            return StructuredRuntime(self.gateway)
        raise ValueError(f"unsupported task runtime {name}")
