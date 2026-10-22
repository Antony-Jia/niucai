"""任务收尾报告：没有结果的结束也要给出结论，并列出产出的文件。"""

from sqlalchemy import select

from niucai.actions.adapters import FakeAdapter
from niucai.control.conversations import ConversationManager
from niucai.storage.db import Action, Artifact, Message, Task
from niucai.worker import Worker


class Gateway:
    def __init__(self, text=None, fail=False):
        self.text, self.fail, self.calls = text, fail, []

    async def chat(self, role, messages, task_id, **extra):
        self.calls.append((str(role), messages))
        if self.fail:
            raise RuntimeError("model down")
        return {"choices": [{"message": {"content": self.text}}]}


class Runtime:
    def __init__(self, outcome, gateway=None, seed=None):
        self.outcome, self.gateway, self.seed = outcome, gateway, seed

    async def run(self, worker, task):
        if self.seed:
            self.seed(worker, task)
        return self.outcome


def seed_browsing(worker, task):
    with worker.db.sessions.begin() as s:
        s.add_all(
            [
                Action(
                    task_id=task.id,
                    agent_id="main",
                    idempotency_key=f"{task.id}:1",
                    spec={"type": "browser.navigate", "url": "https://news.example.com/"},
                    risk="LOW",
                    status="SUCCEEDED",
                    result={"url": "https://news.example.com/"},
                ),
                Action(
                    task_id=task.id,
                    agent_id="main",
                    idempotency_key=f"{task.id}:2",
                    spec={"type": "browser.snapshot"},
                    risk="LOW",
                    status="SUCCEEDED",
                    result={"url": "https://news.example.com/", "title": "今日新闻", "text": "头条 AAA 发布"},
                ),
                Action(
                    task_id=task.id,
                    agent_id="main",
                    idempotency_key=f"{task.id}:3",
                    spec={"type": "shell.exec", "argv": ["curl", "-o", "report.pdf", "https://x.test/a.pdf"]},
                    risk="HIGH",
                    status="SUCCEEDED",
                    result={"exit_code": 0, "output": ""},
                ),
                Artifact(task_id=task.id, path="report.pdf", media_type="application/pdf"),
            ]
        )


def start(setup, runtime):
    db, settings, _, _ = setup
    conversation, _, task = ConversationManager(db).send(None, "查询今天的新闻")
    return db, Worker(db, settings, runtime, FakeAdapter()), conversation, task


def messages(db):
    with db.sessions() as s:
        return [m for m in s.scalars(select(Message).order_by(Message.sequence)) if m.role == "assistant"]


async def test_budget_exhaustion_leaves_a_conclusion_with_files(setup):
    gateway = Gateway("已查到：头条 AAA 发布。")
    outcome = {"status": "WAITING_HUMAN", "checkpoint": {"reason": "step budget exhausted"}}
    db, worker, _, task = start(setup, Runtime(outcome, gateway, seed_browsing))
    await worker.run_once()
    (message,) = messages(db)
    assert "头条 AAA" in message.content
    assert "https://news.example.com/" in message.content
    assert "`report.pdf`" in message.content and "curl" in message.content
    with db.sessions() as s:
        current = s.get(Task, task.id)
        assert current.status == "WAITING_HUMAN"
        assert "report_message" not in current.checkpoint
        assert current.checkpoint["report"]["files"][0]["path"] == "report.pdf"
    assert gateway.calls[0][0] == "summarizer"


async def test_budget_exhaustion_falls_back_without_model(setup):
    outcome = {"status": "WAITING_HUMAN", "checkpoint": {"reason": "model turn budget exhausted"}}
    db, worker, _, _ = start(setup, Runtime(outcome, Gateway(fail=True), seed_browsing))
    await worker.run_once()
    (message,) = messages(db)
    assert "执行次数已达到上限" in message.content and "头条 AAA" in message.content


async def test_completion_keeps_answer_and_appends_facts(setup):
    db, worker, _, _ = start(setup, Runtime({"status": "COMPLETED", "checkpoint": {"result": "好"}}, None))
    await worker.run_once()
    assert messages(db)[0].content.startswith("好")

    answer = "今天的头条是 AAA 发布了新产品，另有两条行业快讯，详见下方来源页面。"
    gateway = Gateway("不应被调用")
    _, worker, _, _ = start(
        setup, Runtime({"status": "COMPLETED", "checkpoint": {"result": answer}}, gateway, seed_browsing)
    )
    await worker.run_once()
    content = messages(db)[-1].content
    assert content.startswith(answer) and "`report.pdf`" in content
    assert not gateway.calls


async def test_failed_run_reports_reason_once(setup):
    outcome = {"status": "FAILED", "checkpoint": {"detail": "浏览器崩溃"}}
    gateway = Gateway("不应被调用")
    db, worker, _, task = start(setup, Runtime(outcome, gateway))
    await worker.run_once()
    (message,) = messages(db)
    assert "浏览器崩溃" in message.content and not gateway.calls
    with db.sessions() as s:
        assert s.get(Task, task.id).status == "FAILED"


async def test_empty_completion_gets_a_model_conclusion(setup):
    gateway = Gateway("已完成：头条是 AAA。")
    db, worker, _, _ = start(
        setup, Runtime({"status": "COMPLETED", "checkpoint": {}}, gateway, seed_browsing)
    )
    await worker.run_once()
    content = messages(db)[0].content
    assert content.startswith("已完成：头条是 AAA。") and "`report.pdf`" in content
