from fastapi.testclient import TestClient
from sqlalchemy import select

from niucai.actions.adapters import FakeAdapter
from niucai.api.app import create_app
from niucai.control.artifacts import snapshot
from niucai.control.collaboration import CollaborationManager
from niucai.control.conversations import ConversationManager
from niucai.domain.schemas import TaskCreate
from niucai.storage.db import Agent, Message, Task, Workflow, WorkItem

AGENTS = ["crawler", "cleaner", "analyst", "writer"]


def prepare(setup):
    db, settings, tasks, _ = setup
    settings.multi_bot_enabled = True
    with db.sessions.begin() as session:
        for ident in AGENTS:
            session.add(Agent(id=ident, name=ident, definition={"tools": ["files.*", "bot_*"]}))
    client = TestClient(
        create_app(settings, db, FakeAdapter()), headers={"Authorization": "Bearer " + "x" * 40}
    )
    return db, settings, tasks, client


def finish(db, tasks, task, content, status="COMPLETED"):
    inbox = ConversationManager(db)
    for message in inbox.pending(task.id):
        inbox.acknowledge(task.id, task.run_token, message.id, message.id)
    return tasks.update(task.id, task.run_token, status=status, checkpoint={"result": content})


def statuses(db, workflow_id):
    with db.sessions() as session:
        workflow = session.get(Workflow, workflow_id)
        stages = {s["work_item_id"]: s["key"] for s in workflow.limits["stages"]}
        items = session.scalars(select(WorkItem).where(WorkItem.workflow_id == workflow_id))
        return workflow.status, {stages[i.id]: i.status for i in items}


def inputs(db, task_id):
    return [m.content for m in ConversationManager(db).pending(task_id)]


STAGES = [
    {"key": "crawl", "agent_id": "crawler", "goal": "抓取商品数据"},
    {"key": "clean", "agent_id": "cleaner", "goal": "清洗数据", "depends_on": ["crawl"]},
    {"key": "stats", "agent_id": "analyst", "goal": "统计价格", "depends_on": ["crawl"]},
    {
        "key": "report",
        "agent_id": "writer",
        "goal": "写报告",
        "depends_on": ["clean", "stats"],
        "output_requirements": "Markdown",
    },
]


def test_pipeline_runs_stages_in_dependency_order(setup):
    db, settings, tasks, client = prepare(setup)
    response = client.post("/api/workflows", json={"goal": "商品调研", "stages": STAGES})
    assert response.status_code == 201, response.text
    workflow = response.json()
    assert statuses(db, workflow["id"])[1] == {
        "crawl": "QUEUED",
        "clean": "WAITING_BOT",
        "stats": "WAITING_BOT",
        "report": "WAITING_BOT",
    }
    crawl = tasks.claim()
    assert crawl.agent_id == "crawler" and tasks.claim() is None
    with db.sessions.begin() as session:
        artifact = snapshot(session, settings, crawl.id, b"sku,price\n1,9.9\n", "text/csv")
        artifact_id, artifact_path = artifact.id, artifact.path
    finish(db, tasks, crawl, "抓取 42 条记录")
    released = [tasks.claim(), tasks.claim()]
    assert {t.agent_id for t in released} == {"cleaner", "analyst"} and tasks.claim() is None
    for task in released:
        (assignment,) = inputs(db, task.id)
        assert "上游阶段 crawl" in assignment and "抓取 42 条记录" in assignment
        assert artifact_id in assignment and artifact_path in assignment
        with db.sessions() as session:
            item = session.scalar(select(WorkItem).where(WorkItem.task_id == task.id))
            assert item.wait == {} and [a["id"] for a in item.artifacts] == [artifact_id]
    finish(db, tasks, released[0], "第一份结论")
    assert tasks.claim() is None  # report 仍等待另一个上游
    finish(db, tasks, released[1], "第二份结论")
    report = tasks.claim()
    assert report.agent_id == "writer"
    (assignment,) = inputs(db, report.id)
    assert "第一份结论" in assignment and "第二份结论" in assignment and "Markdown" in assignment
    finish(db, tasks, report, "最终报告")
    status, items = statuses(db, workflow["id"])
    assert status == "COMPLETED" and set(items.values()) == {"COMPLETED"}
    with db.sessions() as session:
        overview = session.get(Workflow, workflow["id"]).origin_conversation_id
        results = session.scalars(
            select(Message.content).where(Message.conversation_id == overview, Message.kind == "RESULT")
        ).all()
        assert "最终报告" in results and len(results) == 4


def test_pipeline_failure_cancels_downstream(setup):
    db, _, tasks, client = prepare(setup)
    workflow = client.post("/api/workflows", json={"goal": "g", "stages": STAGES}).json()
    finish(db, tasks, tasks.claim(), "站点拒绝访问", status="FAILED")
    status, items = statuses(db, workflow["id"])
    assert status == "FAILED"
    assert items == {"crawl": "FAILED", "clean": "CANCELLED", "stats": "CANCELLED", "report": "CANCELLED"}
    assert tasks.claim() is None


def test_pipeline_validation(setup):
    _, _, _, client = prepare(setup)
    cycle = [
        {"key": "a", "agent_id": "crawler", "goal": "a", "depends_on": ["b"]},
        {"key": "b", "agent_id": "cleaner", "goal": "b", "depends_on": ["a"]},
    ]
    for stages in (
        cycle,
        [{"key": "a", "agent_id": "crawler", "goal": "a", "depends_on": ["missing"]}],
        [{"key": "a", "agent_id": "crawler", "goal": "a"}, {"key": "a", "agent_id": "writer", "goal": "b"}],
    ):
        assert client.post("/api/workflows", json={"goal": "g", "stages": stages}).status_code == 422
    routed = {"goal": "g", "stages": STAGES, "routes": [["crawler", "writer"]], "final_agent_ids": ["writer"]}
    assert client.post("/api/workflows", json=routed).status_code == 422
    unknown = [{"key": "a", "agent_id": "nobody", "goal": "a"}]
    assert client.post("/api/workflows", json={"goal": "g", "stages": unknown}).status_code == 404


def test_bot_pipeline_tool_is_idempotent_and_waitable(setup):
    db, settings, tasks, _ = prepare(setup)
    tasks.create(TaskCreate(title="lead", goal="商品调研"), unified=True)
    root = tasks.claim()
    sequence = 0
    for message in ConversationManager(db).pending(root.id):
        ConversationManager(db).acknowledge(root.id, root.run_token, message.id, message.id)
        sequence = max(sequence, message.sequence)
    manager = CollaborationManager(db, settings)
    stages = STAGES[:2]
    created = manager.pipeline(root.id, root.run_token, "tool-1", stages, sequence)
    again = manager.pipeline(root.id, root.run_token, "tool-1", stages, sequence)
    assert again["stages"] == created["stages"]
    wait = manager.wait(root.id, root.run_token, "wait-1", created["handoff_ids"])
    tasks.update(root.id, root.run_token, status="WAITING_BOT", checkpoint=wait["wait"])
    crawl = tasks.claim()
    assert crawl.agent_id == "crawler"
    with db.sessions() as session:
        # 阶段权限不超过发起者：委派链记录在执行检查点中。
        assert session.get(Task, crawl.id).checkpoint["authorization_chain"] == [["*"]]
    finish(db, tasks, crawl, "rows")
    clean = tasks.claim()
    assert clean.agent_id == "cleaner"
    finish(db, tasks, clean, "clean rows")
    resumed = tasks.claim()
    assert resumed.id == root.id
    with db.sessions() as session:
        results = session.scalars(
            select(Message.content).where(Message.task_id == root.id, Message.kind == "RESULT")
        ).all()
        assert set(results) == {"rows", "clean rows"}
