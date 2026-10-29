import asyncio
import json
from datetime import timedelta

from fastapi.testclient import TestClient
from test_cards import board, bot

from niucai.api.app import create_app
from niucai.control.orchestrator import Orchestrator
from niucai.storage.db import Card, Memory, Message, OrchestratorRun, now


class ScriptedGateway:
    """按顺序返回预设的模型回复；每个回复是工具调用列表或最终文本（fake，仅测试）。"""

    def __init__(self, *replies):
        self.replies, self.requests = list(replies), []

    async def chat(self, role, messages, task_id, **extra):
        self.requests.append({"role": role, "messages": [dict(m) for m in messages], **extra})
        reply = self.replies.pop(0) if self.replies else "done"
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, str):
            message = {"role": "assistant", "content": reply}
        else:
            message = {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": f"call-{i}",
                        "type": "function",
                        "function": {"name": n, "arguments": json.dumps(a)},
                    }
                    for i, (n, a) in enumerate(reply)
                ],
            }
        return {"choices": [{"message": message}], "usage": {"prompt_tokens": 10, "completion_tokens": 2}}


def client(setup):
    db, settings, *_ = setup
    settings.orchestrator_enabled = True
    api = TestClient(create_app(settings, db))
    return api, {"Authorization": "Bearer " + "x" * 40}


def goal(api, headers, message="把登录页修好"):
    response = api.post("/api/goals", json={"message": message}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def run(orchestrator):
    return asyncio.run(orchestrator.run_once())


def runs(db, goal_id):
    with db.sessions() as s:
        return (
            s.query(OrchestratorRun)
            .filter(OrchestratorRun.conversation_id == goal_id)
            .order_by(OrchestratorRun.created_at)
            .all()
        )


def test_goal_message_runs_planner_tool_loop(setup):
    db, settings, remote, cards, node = board(setup)
    coder = bot(cards, node, role="前端开发", tags=["web"])
    api, headers = client(setup)
    goal_id = goal(api, headers)
    [pending] = runs(db, goal_id)
    assert pending.status == "PENDING" and pending.triggers[0]["kind"] == "message"
    gateway = ScriptedGateway(
        [("bots_list", {}), ("card_create", {"title": "修登录", "goal": "修复报错", "bot": "coder"})],
        [("goal_reply", {"text": "已交给 @coder"})],
        "本轮结束",
    )
    orchestrator = Orchestrator(db, settings, gateway)
    assert run(orchestrator) is True
    assert run(orchestrator) is False
    first = gateway.requests[0]
    assert first["tools"] and first["tool_choice"] == "auto"
    package = json.loads(first["messages"][1]["content"])
    assert package["bots"][0]["name"] == "coder" and package["bots"][0]["node_online"] is True
    assert package["messages"][0]["content"] == "把登录页修好"
    with db.sessions() as s:
        [card] = s.query(Card).filter(Card.conversation_id == goal_id).all()
        assert card.assignee_bot_id == coder["id"] and card.created_by == "kernel" and card.status == "READY"
        [done] = s.query(OrchestratorRun).filter(OrchestratorRun.conversation_id == goal_id).all()
        assert done.status == "COMPLETED" and len(done.steps) == 3 and done.output == "本轮结束"
        assert done.usage["prompt_tokens"] == 30
        reply = s.query(Message).filter(Message.conversation_id == goal_id, Message.kind == "REPLY").one()
        assert reply.source == "KERNEL"
    detail = api.get(f"/api/goals/{goal_id}", headers=headers).json()
    assert [c["title"] for c in detail["board"]["cards"]] == ["修登录"]
    assert detail["runs"][0]["tool_calls"] == 3 and "steps" not in detail["runs"][0]
    full = api.get(f"/api/orchestrator/runs/{detail['runs'][0]['id']}", headers=headers).json()
    assert full["steps"][1]["tool"] == "card_create"
    listing = api.get("/api/goals", headers=headers).json()
    assert listing[0]["id"] == goal_id and listing[0]["cards"] == {"READY": 1}


def test_card_events_trigger_review_and_card_memory(setup):
    db, settings, remote, cards, node = board(setup)
    bot(cards, node)
    api, headers = client(setup)
    goal_id = goal(api, headers)
    with db.sessions.begin() as s:
        s.query(OrchestratorRun).delete()
    task = cards.create_card(
        {"title": "写文档", "goal": "README", "conversation_id": goal_id, "review": "llm"}
    )
    claimed = remote.claim(node["id"])
    remote.finish(node["id"], claimed["id"], claimed["attempt_id"], "COMPLETED", {"answer": "写好了"})
    [pending] = runs(db, goal_id)
    assert [t["kind"] for t in pending.triggers] == ["review"]
    gateway = ScriptedGateway(
        [("card_review", {"card_id": task["id"], "verdict": "accept"})],
        [("goal_reply", {"text": "文档已完成", "final": True})],
    )
    run(Orchestrator(db, settings, gateway))
    with db.sessions() as s:
        assert s.get(Card, task["id"]).status == "DONE"
        memory = s.query(Memory).filter(Memory.kind == "card", Memory.ref_id == task["id"]).one()
        assert "写好了" in memory.content and "done" in memory.tags
        assert s.query(Memory).filter(Memory.kind == "goal", Memory.ref_id == goal_id).count() == 1
        # 编排自己的验收不再触发新运行。
        assert s.query(OrchestratorRun).filter(OrchestratorRun.status == "PENDING").count() == 0
    assert api.get(f"/api/goals/{goal_id}", headers=headers).json()["status"] == "DONE"
    found = api.get("/api/memories/search", params={"q": "写好了"}, headers=headers).json()
    assert [m["ref_id"] for m in found] == [task["id"]]
    edited = api.patch(f"/api/memories/{found[0]['id']}", json={"content": "改过"}, headers=headers)
    assert edited.json()["content"] == "改过"
    assert api.delete(f"/api/memories/{found[0]['id']}", headers=headers).status_code == 200
    assert api.get("/api/memories/search", params={"q": "改过"}, headers=headers).json() == []


def test_blocked_question_and_proposal_are_decided(setup):
    db, settings, remote, cards, node = board(setup)
    bot(cards, node)
    api, headers = client(setup)
    goal_id = goal(api, headers)
    with db.sessions.begin() as s:
        s.query(OrchestratorRun).delete()
    task = cards.create_card({"title": "部署", "goal": "上线", "conversation_id": goal_id})
    claimed = remote.claim(node["id"])
    call = lambda tool, args: cards.board_call(  # noqa: E731
        node["id"], claimed["id"], claimed["board_token"], tool, args
    )
    call("card_propose", {"title": "补监控", "goal": "加告警"})
    call("card_block", {"question": "用哪个环境？", "mention": "kernel"})
    remote.finish(node["id"], claimed["id"], claimed["attempt_id"], "COMPLETED", {"answer": "等回答"})
    [pending] = runs(db, goal_id)
    assert [t["kind"] for t in pending.triggers] == ["proposal", "blocked"]
    proposal_seq = pending.triggers[0]["seq"]
    gateway = ScriptedGateway(
        [
            ("card_comment", {"card_id": task["id"], "body": "用 staging"}),
            ("proposal_decide", {"card_id": task["id"], "seq": proposal_seq, "decision": "accept"}),
            ("proposal_decide", {"card_id": task["id"], "seq": proposal_seq, "decision": "accept"}),
            ("card_get", {"card_id": "nope"}),
            ("no_such_tool", {}),
        ],
    )
    run(Orchestrator(db, settings, gateway))
    with db.sessions() as s:
        assert s.get(Card, task["id"]).status == "READY"
        titles = sorted(c.title for c in s.query(Card).filter(Card.conversation_id == goal_id))
        assert titles == ["补监控", "部署"]
        follow_up = s.query(Card).filter(Card.title == "补监控").one()
        assert follow_up.depends_on == [task["id"]]  # 采纳的提议默认排在原卡之后
        [done] = runs(db, goal_id)
        results = [json.loads(step["result"]) for step in done.steps]
        assert results[0]["answered"] is True
        assert "error" in results[2] and "error" in results[3] and "unknown tool" in results[4]["error"]


def test_one_run_per_goal_lease_recovery_and_failures(setup):
    db, settings, remote, cards, node = board(setup)
    api, headers = client(setup)
    goal_id = goal(api, headers)
    orchestrator = Orchestrator(db, settings, ScriptedGateway(RuntimeError("model down")))
    claimed = orchestrator.claim()
    assert claimed["conversation_id"] == goal_id
    api.post(f"/api/goals/{goal_id}/messages", json={"content": "再加一条"}, headers=headers)
    assert orchestrator.claim() is None  # 同一 Goal 已有 RUNNING
    with db.sessions.begin() as s:
        s.get(OrchestratorRun, claimed["id"]).lease_until = now() - timedelta(seconds=1)
    second = orchestrator.claim()
    assert second and second["triggers"][0]["kind"] == "message"
    asyncio.run(orchestrator.execute(second))
    with db.sessions() as s:
        assert s.get(OrchestratorRun, claimed["id"]).error == "lease expired"
        failed = s.get(OrchestratorRun, second["id"])
        assert failed.status == "FAILED" and "model down" in failed.error
    settings.orchestrator_runs_per_hour = 2
    api.post(f"/api/goals/{goal_id}/run", headers=headers)
    assert orchestrator.claim() is None  # 每小时运行数已达上限


def test_crashed_run_recovers_without_new_events(setup):
    db, settings, remote, cards, node = board(setup)
    api, headers = client(setup)
    goal(api, headers)
    orchestrator = Orchestrator(db, settings, ScriptedGateway("done"))
    for attempt in range(3):
        claimed = orchestrator.claim()
        assert claimed and claimed["triggers"][-1]["kind"] in {"message", "recovered"}
        assert any(t["kind"] == "message" for t in claimed["triggers"])
        with db.sessions.begin() as s:  # Worker 在运行途中崩溃，之后没有任何新事件
            s.get(OrchestratorRun, claimed["id"]).lease_until = now() - timedelta(seconds=1)
        if attempt == 0:
            first = claimed
    # 崩溃两次后不再自动重试，避免崩溃循环。
    assert orchestrator.claim() is None
    # 过期的执行者不能再写入已被回收的运行。
    orchestrator.close(first["id"], "COMPLETED", [], {}, "late", None)
    with db.sessions() as s:
        late = s.get(OrchestratorRun, first["id"])
        assert late.status == "FAILED" and late.error == "lease expired"


def test_goal_api_guards(setup):
    db, settings, *_ = setup
    api = TestClient(create_app(settings, db))
    headers = {"Authorization": "Bearer " + "x" * 40}
    assert api.post("/api/goals", json={"message": "hi"}, headers=headers).status_code == 409
    settings.orchestrator_enabled = True
    goal_id = goal(api, headers)
    sent = api.post(f"/api/conversations/{goal_id}/messages", json={"content": "x"}, headers=headers)
    assert sent.status_code == 409
    first = api.post(
        f"/api/goals/{goal_id}/messages", json={"content": "a", "client_id": "c1"}, headers=headers
    )
    again = api.post(
        f"/api/goals/{goal_id}/messages", json={"content": "a", "client_id": "c1"}, headers=headers
    )
    assert again.json() == {"message_id": first.json()["message_id"], "duplicate": True}
    assert api.get("/api/goals/missing", headers=headers).status_code == 404


def test_inbox_is_scoped_to_the_cards_goal(setup):
    db, settings, remote, cards, node = board(setup)
    bot(cards, node)
    api, headers = client(setup)
    mine, other = goal(api, headers, "目标一"), goal(api, headers, "目标二")
    task = cards.create_card({"title": "a", "goal": "x", "conversation_id": mine})
    elsewhere = cards.create_card({"title": "b", "goal": "y", "conversation_id": other, "draft": True})
    loose = cards.create_card({"title": "c", "goal": "z", "draft": True})
    cards.comment(elsewhere["id"], "@coder 别的目标的机密")
    cards.comment(loose["id"], "@coder 无目标的卡片")
    cards.comment(task["id"], "@coder 本目标")
    claimed = remote.claim(node["id"])
    assert claimed["card_id"] == task["id"]
    inbox = cards.board_call(node["id"], claimed["id"], claimed["board_token"], "inbox", {})
    assert [m["body"] for m in inbox["messages"]] == ["@coder 本目标"]


def test_card_update_requires_version(setup):
    db, settings, remote, cards, node = board(setup)
    api, headers = client(setup)
    goal_id = goal(api, headers)
    task = cards.create_card({"title": "a", "goal": "x", "conversation_id": goal_id, "draft": True})
    with db.sessions.begin() as s:
        s.query(OrchestratorRun).delete()
    gateway = ScriptedGateway(
        [
            ("card_update", {"card_id": task["id"], "title": "无版本"}),
            ("card_update", {"card_id": task["id"], "version": task["version"] + 1, "title": "旧版本"}),
            ("card_update", {"card_id": task["id"], "version": task["version"], "title": "新标题"}),
        ],
    )
    orchestrator = Orchestrator(db, settings, gateway)
    api.post(f"/api/goals/{goal_id}/run", headers=headers)
    run(orchestrator)
    [done] = runs(db, goal_id)
    results = [json.loads(step["result"]) for step in done.steps]
    assert "version" in results[0]["error"] and "error" in results[1]
    with db.sessions() as s:
        assert s.get(Card, task["id"]).title == "新标题"
