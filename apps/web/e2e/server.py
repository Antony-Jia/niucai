"""CI-only fixture: real database/API, deterministic model, no device or provider calls."""

from niucai.api.app import create_app
from niucai.config import Settings
from niucai.control.tasks import TaskManager
from niucai.domain.schemas import TaskCreate
from niucai.storage.db import Action, Agent, Approval, Base, Computer, Database

settings = Settings()
if settings.database_url not in {"sqlite:///./e2e.db", "sqlite:///./unified-e2e.db"}:
    raise RuntimeError("mobile fixture requires the dedicated e2e.db")
db = Database(settings.database_url)
db.create_schema()
with db.sessions.begin() as s:
    for table in reversed(Base.metadata.sorted_tables):
        s.execute(table.delete())
    if not s.get(Agent, "main"):
        s.add(Agent(id="main", name="Main", definition={}))
    if not s.get(Computer, "ci-computer"):
        s.add(Computer(id="ci-computer", name="CI Cloud Computer", status="ONLINE"))
manager = TaskManager(db, settings)
task = manager.create(TaskCreate(title="待审批报告", goal="Write report"))
with db.sessions.begin() as s:
    for name in [
        f"{project}-{decision}"
        for project in ("android-pixel", "android-small", "android-landscape")
        for decision in ("approve", "deny")
    ]:
        if not s.get(Action, name):
            action = Action(
                id=name,
                task_id=task.id,
                agent_id="main",
                spec={
                    "type": "files.write",
                    "path": name + ".txt",
                    "content": "CI fixture",
                },
                status="WAITING_APPROVAL",
                risk="HIGH",
                idempotency_key=name,
            )
            s.add(action)
            s.flush()
            s.add(Approval(id=name, action_id=name))


class Reply:
    async def chat(self, role, messages, task_id):
        return {
            "choices": [
                {"message": {"content": "已记录你的目标。可创建 Task 持续执行。"}}
            ]
        }


app = create_app(settings, db, Reply())
