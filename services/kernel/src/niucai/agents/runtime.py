from niucai.domain.schemas import Decision, Plan, Role


class StructuredRuntime:
    """Replaceable decision harness; all state and side effects belong to the Kernel."""

    def __init__(self, gateway):
        self.gateway = gateway

    async def plan(self, package, task_id):
        return await self.gateway.structured(Role.PLANNER, package, Plan, task_id)

    async def decide(self, package, task_id):
        decision = await self.gateway.structured(Role.EXECUTOR, package, Decision, task_id)
        return decision.validate_action()
