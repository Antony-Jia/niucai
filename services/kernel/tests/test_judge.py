import asyncio
import json

from fastapi.testclient import TestClient
from sqlalchemy import select
from test_cards import board, bot, card

from niucai.api.app import create_app
from niucai.control.judge import Judge
from niucai.storage.db import CardEvent, JudgeRun, Memory

AUTH = {"Authorization": "Bearer " + "x" * 40}


class JudgeGateway:
    """按顺序返回预设的裁判回复（字符串或异常）；fake，仅测试。"""

    def __init__(self, *replies):
        self.replies, self.requests = list(replies), []

    async def chat(self, role, messages, task_id, **extra):
        self.requests.append([dict(m) for m in messages])
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, dict):
            reply = json.dumps(reply, ensure_ascii=False)
        return {
            "choices": [{"message": {"role": "assistant", "content": reply}}],
            "usage": {"prompt_tokens": 5},
        }


def setup_board(setup):
    db, settings, remote, cards, node = board(setup, slots=8)
    for index in range(6):  # 同一 (Bot, Goal) 同时只跑一张卡，多备几个 Bot 让测试里的卡片可以并存
        bot(cards, node, name=f"coder{index}")
    return db, settings, remote, cards, node


def block(remote, cards, node, title, question, **args):
    """新建一张卡片并让 Bot 认领、提问、收尾（卡片进入 BLOCKED）。"""
    task = card(cards, title, **args.pop("card", {}))
    claimed = remote.claim(node["id"])
    assert claimed["card_id"] == task["id"]
    result = cards.board_call(
        node["id"], claimed["id"], claimed["board_token"], "card_block", {"question": question, **args}
    )
    remote.finish(node["id"], claimed["id"], claimed["attempt_id"], "COMPLETED", {"answer": "waiting"})
    return task, claimed, result


def judge_once(db, settings, gateway):
    judge = Judge(db, settings, gateway)
    assert asyncio.run(judge.run_once())


def events(db, card_id, kind=None):
    with db.sessions() as s:
        query = select(CardEvent).where(CardEvent.card_id == card_id).order_by(CardEvent.id)
        if kind:
            query = query.where(CardEvent.kind == kind)
        return [
            {"kind": e.kind, "actor": e.actor, "body": e.body, "mentions": e.mentions, "data": e.data}
            for e in s.scalars(query)
        ]


def user_mentions(db, card_id):
    return [e for e in events(db, card_id) if "user" in (e["mentions"] or [])]


def decision_ids(db):
    with db.sessions() as s:
        return [m.id for m in s.scalars(select(Memory).where(Memory.kind == "decision"))]


def seed_decision(setup):
    """用户先回答过一个问题：产生一条 decision 记录，返回 (记录 ID, 其它上下文)。"""
    db, settings, remote, cards, node = setup_board(setup)
    task, claimed, _ = block(remote, cards, node, "旧任务", "用哪个数据库？")
    cards.action(task["id"], "answer", body="用 PostgreSQL，别用 SQLite")
    [record] = decision_ids(db)
    return db, settings, remote, cards, node, record


def test_user_answer_writes_decision_record(setup):
    db, settings, remote, cards, node, record = seed_decision(setup)
    with db.sessions() as s:
        memory = s.get(Memory, record)
        assert memory.kind == "decision"
        meta = memory.meta
        assert meta["question"] == "用哪个数据库？" and meta["answer"] == "用 PostgreSQL，别用 SQLite"
        assert meta["scope"] == "global" and meta["principle"] is False and meta["corrects"] is None


def test_decided_cites_record_and_resume_prompt_says_judge_answered(setup):
    db, settings, remote, cards, node, record = seed_decision(setup)
    remote.claim(node["id"])  # 旧卡片续跑
    task, claimed, result = block(remote, cards, node, "新任务", "这次存储选什么数据库？")
    assert "裁判" in result["next"]
    # held 之前用户收不到 @user，卡片已是 BLOCKED，视图标出裁判处理中。
    assert user_mentions(db, task["id"]) == []
    assert cards.get(task["id"])["status"] == "BLOCKED"
    view = next(c for c in cards.board()["cards"] if c["id"] == task["id"])
    assert view["judge"]["status"] == "PENDING"

    gateway = JudgeGateway(
        {"verdict": "decided", "record_ids": [record], "explanation": "同类问题", "answer": "用 PostgreSQL"}
    )
    judge_once(db, settings, gateway)

    resumed = remote.claim(node["id"])
    assert f"裁判依据记录 {record} 代答" in resumed["prompt"]
    assert "不是用户" in resumed["prompt"]
    assert user_mentions(db, task["id"]) == []
    with db.sessions() as s:
        run = s.scalar(select(JudgeRun).where(JudgeRun.card_id == task["id"]))
        assert run.verdict == "decided" and run.cited_ids == [record] and run.status == "DONE"
        assert record in run.record_ids
    # 记录原样交给模型，且包含本次问题。
    payload = json.loads(gateway.requests[0][1]["content"])
    assert payload["question"] == "这次存储选什么数据库？" and payload["records"][0]["id"] == record


def test_returned_explains_and_resumes_without_user(setup):
    db, settings, remote, cards, node = setup_board(setup)
    task, claimed, _ = block(remote, cards, node, "t", "既然已经没有测试了，要不要补？")
    judge_once(db, settings, JudgeGateway({"verdict": "returned", "explanation": "前提不成立：仓库有测试"}))
    resumed = remote.claim(node["id"])
    assert "前提不成立" in resumed["prompt"] and "裁判退回" in resumed["prompt"]
    assert user_mentions(db, task["id"]) == []


def test_new_question_is_held_then_goes_to_user(setup):
    db, settings, remote, cards, node = setup_board(setup)
    task, claimed, _ = block(remote, cards, node, "t", "该用蓝色还是绿色主题？")
    assert user_mentions(db, task["id"]) == []
    judge_once(db, settings, JudgeGateway({"verdict": "held", "record_ids": [], "explanation": "没有先例"}))
    [asked] = user_mentions(db, task["id"])
    assert asked["kind"] == "question" and "该用蓝色还是绿色主题？" in asked["body"]
    assert cards.get(task["id"])["status"] == "BLOCKED"
    # 用户回答后照常续跑，并且问题取自原问题。
    cards.action(task["id"], "answer", body="蓝色")
    with db.sessions() as s:
        memory = s.scalars(select(Memory).where(Memory.kind == "decision")).one()
        assert memory.meta["question"] == "该用蓝色还是绿色主题？"


def test_authorization_goes_straight_to_user(setup):
    db, settings, remote, cards, node = setup_board(setup)
    task, claimed, _ = block(remote, cards, node, "t", "可以删除生产库吗？", category="authorization")
    assert len(user_mentions(db, task["id"])) == 1
    with db.sessions() as s:
        assert s.scalar(select(JudgeRun)) is None
    cards.action(task["id"], "answer", body="可以")
    assert decision_ids(db) == []  # 授权类回答不当先例


def test_invalid_category_rejected(setup):
    db, settings, remote, cards, node = setup_board(setup)
    card(cards, "t")
    claimed = remote.claim(node["id"])
    try:
        cards.board_call(
            node["id"],
            claimed["id"],
            claimed["board_token"],
            "card_block",
            {"question": "q", "category": "x"},
        )
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_model_failures_and_bad_output_degrade_to_held(setup):
    db, settings, remote, cards, node = setup_board(setup)
    cases = [RuntimeError("model down"), "这不是 JSON", {"verdict": "maybe"}]
    for index, reply in enumerate(cases):
        task, claimed, _ = block(remote, cards, node, f"t{index}", f"问题 {index}？")
        judge_once(db, settings, JudgeGateway(reply))
        assert len(user_mentions(db, task["id"])) == 1, reply
        with db.sessions() as s:
            run = s.scalar(select(JudgeRun).where(JudgeRun.card_id == task["id"]))
            assert run.verdict == "held" and run.status == "FAILED" and run.error


def test_decided_with_unknown_record_degrades_to_held(setup):
    db, settings, remote, cards, node, record = seed_decision(setup)
    remote.claim(node["id"])
    task, claimed, _ = block(remote, cards, node, "新任务", "选什么数据库？")
    judge_once(
        db,
        settings,
        JudgeGateway({"verdict": "decided", "record_ids": ["no-such-record"], "answer": "编的"}),
    )
    assert len(user_mentions(db, task["id"])) == 1
    assert events(db, task["id"], "verdict") == []


def test_deleted_record_after_offer_degrades_to_held(setup):
    db, settings, remote, cards, node, record = seed_decision(setup)
    remote.claim(node["id"])
    task, claimed, _ = block(remote, cards, node, "新任务", "选什么数据库？")

    class Deleting(JudgeGateway):
        async def chat(self, *args, **kwargs):
            with db.sessions.begin() as s:
                s.delete(s.get(Memory, record))
            return await super().chat(*args, **kwargs)

    judge_once(
        db,
        settings,
        Deleting({"verdict": "decided", "record_ids": [record], "answer": "PostgreSQL"}),
    )
    assert len(user_mentions(db, task["id"])) == 1 and events(db, task["id"], "verdict") == []


def test_correction_ranks_first_and_must_be_cited(setup):
    db, settings, remote, cards, node, record = seed_decision(setup)
    remote.claim(node["id"])
    api = TestClient(create_app(settings, db))

    task, claimed, _ = block(remote, cards, node, "二号", "存储选什么数据库？")
    judge_once(
        db,
        settings,
        JudgeGateway({"verdict": "decided", "record_ids": [record], "answer": "PostgreSQL"}),
    )
    [run] = api.get("/api/judge/runs", headers=AUTH).json()["runs"]
    assert run["verdict"] == "decided"
    response = api.post(
        f"/api/judge/runs/{run['id']}/correct", json={"correction": "这个项目用 MySQL"}, headers=AUTH
    )
    assert response.status_code == 201
    correction = response.json()["id"]
    assert response.json()["corrects"] == record
    with db.sessions() as s:
        assert s.get(Memory, record) is not None  # 原记录不删除
        assert s.get(Memory, record).meta["corrected_by"] == correction

    # 相似问题：纠正排在原记录前面，引用被纠正记录而不带纠正则降级。
    remote.claim(node["id"])
    third, _, _ = block(remote, cards, node, "三号", "数据库用什么？")
    gateway = JudgeGateway({"verdict": "decided", "record_ids": [record], "answer": "PostgreSQL"})
    judge_once(db, settings, gateway)
    payload = json.loads(gateway.requests[0][1]["content"])
    assert [r["id"] for r in payload["records"]][:2] == [correction, record]
    assert len(user_mentions(db, third["id"])) == 1  # 只引用旧记录 → 降级

    remote.claim(node["id"])
    fourth, _, _ = block(remote, cards, node, "四号", "数据库选型？")
    judge_once(
        db,
        settings,
        JudgeGateway({"verdict": "decided", "record_ids": [correction], "answer": "MySQL"}),
    )
    resumed = remote.claim(node["id"])
    assert f"裁判依据记录 {correction} 代答" in resumed["prompt"] and "MySQL" in resumed["prompt"]

    again = api.post(f"/api/judge/runs/{run['id']}/correct", json={"correction": "再改一次"}, headers=AUTH)
    assert again.status_code == 409


def test_goal_switch_off_goes_straight_to_user(setup):
    db, settings, remote, cards, node = setup_board(setup)
    settings.orchestrator_enabled = True
    api = TestClient(create_app(settings, db))
    goal = api.post("/api/goals", json={"message": "做点事"}, headers=AUTH).json()["id"]
    assert api.get(f"/api/goals/{goal}", headers=AUTH).json()["judge_enabled"] is None
    patched = api.patch(f"/api/goals/{goal}", json={"judge_enabled": False}, headers=AUTH)
    assert patched.status_code == 200 and patched.json()["judge_enabled"] is False

    task, claimed, _ = block(remote, cards, node, "t", "选哪个？", card={"conversation_id": goal})
    assert len(user_mentions(db, task["id"])) == 1
    with db.sessions() as s:
        assert s.scalar(select(JudgeRun)) is None


def test_global_switch_off_goes_straight_to_user(setup):
    db, settings, remote, cards, node = setup_board(setup)
    settings.judge_enabled = False
    task, claimed, _ = block(remote, cards, node, "t", "选哪个？")
    assert len(user_mentions(db, task["id"])) == 1


def test_user_answer_first_discards_judge_result(setup):
    db, settings, remote, cards, node, record = seed_decision(setup)
    remote.claim(node["id"])
    task, claimed, _ = block(remote, cards, node, "新任务", "选什么数据库？")

    class Racing(JudgeGateway):
        async def chat(self, *args, **kwargs):
            cards.action(task["id"], "answer", body="用户抢先：MySQL")
            return await super().chat(*args, **kwargs)

    judge_once(
        db,
        settings,
        Racing({"verdict": "decided", "record_ids": [record], "answer": "PostgreSQL"}),
    )
    assert events(db, task["id"], "verdict") == []
    assert len([e for e in events(db, task["id"], "answer")]) == 1
    with db.sessions() as s:
        run = s.scalar(select(JudgeRun).where(JudgeRun.card_id == task["id"]))
        assert run.status == "DISCARDED"
    resumed = remote.claim(node["id"])
    assert "用户抢先" in resumed["prompt"] and "裁判依据记录" not in resumed["prompt"]
    assert remote.claim(node["id"]) is None  # 没有重复续跑


def test_user_answer_before_judge_claims_voids_pending_run(setup):
    db, settings, remote, cards, node = setup_board(setup)
    task, claimed, _ = block(remote, cards, node, "t", "选哪个？")
    cards.action(task["id"], "answer", body="选 A")
    with db.sessions() as s:
        assert s.scalar(select(JudgeRun)).status == "DISCARDED"
    assert not asyncio.run(Judge(db, settings, JudgeGateway()).run_once())


def test_runs_api_filters_counts_and_principle(setup):
    db, settings, remote, cards, node, record = seed_decision(setup)
    remote.claim(node["id"])
    api = TestClient(create_app(settings, db))
    task, claimed, _ = block(remote, cards, node, "t", "选哪个主题？")
    judge_once(db, settings, JudgeGateway({"verdict": "held", "explanation": "无先例"}))
    body = api.get("/api/judge/runs", headers=AUTH).json()
    assert body["counts"] == {"decided": 0, "returned": 0, "held": 1} and len(body["runs"]) == 1
    assert api.get("/api/judge/runs?verdict=decided", headers=AUTH).json()["runs"] == []
    assert api.get("/api/judge/runs?verdict=bogus", headers=AUTH).status_code == 422

    marked = api.patch(f"/api/judge/decisions/{record}", json={"principle": True}, headers=AUTH)
    assert marked.status_code == 200 and marked.json()["principle"] is True
    listed = api.get("/api/judge/decisions", headers=AUTH).json()["decisions"]
    assert listed[0]["id"] == record or any(d["id"] == record for d in listed)


def test_held_run_cannot_be_corrected_and_keeps_related_records(setup):
    db, settings, remote, cards, node, record = seed_decision(setup)
    remote.claim(node["id"])
    api = TestClient(create_app(settings, db))
    task, claimed, _ = block(remote, cards, node, "缓存", "缓存用 Redis 吗？")
    judge_once(db, settings, JudgeGateway({"verdict": "held", "record_ids": [record], "explanation": "相近"}))
    [run] = api.get("/api/judge/runs", headers=AUTH).json()["runs"]
    assert run["verdict"] == "held" and run["cited_ids"] == [record]
    cards.action(task["id"], "answer", body="不用 Redis")
    response = api.post(f"/api/judge/runs/{run['id']}/correct", json={"correction": "用 Redis"}, headers=AUTH)
    assert response.status_code == 409
    with db.sessions() as s:
        assert s.get(Memory, record).meta["corrected_by"] is None  # 相近记录不能被误标为已纠正


def test_card_judge_field_follows_the_current_question(setup):
    db, settings, remote, cards, node, record = seed_decision(setup)
    remote.claim(node["id"])

    def judge_view(card_id):
        return next(c for c in cards.board()["cards"] if c["id"] == card_id)["judge"]

    task, claimed, _ = block(remote, cards, node, "新任务", "这次用什么数据库？")
    judge_once(
        db, settings, JudgeGateway({"verdict": "decided", "record_ids": [record], "answer": "PostgreSQL"})
    )
    assert judge_view(task["id"])["verdict"] == "decided"  # 续接排队时仍显示代答
    resumed = remote.claim(node["id"])
    cards.board_call(
        node["id"],
        resumed["id"],
        resumed["board_token"],
        "card_block",
        {"question": "可以付款吗？", "category": "authorization"},
    )
    remote.finish(node["id"], resumed["id"], resumed["attempt_id"], "COMPLETED", {"answer": "waiting"})
    assert judge_view(task["id"]) is None  # 授权类提问直达用户，不显示上一个问题的裁决

    cards.action(task["id"], "answer", body="可以")
    resumed = remote.claim(node["id"])
    cards.board_call(
        node["id"], resumed["id"], resumed["board_token"], "card_block", {"question": "选什么主题？"}
    )
    remote.finish(node["id"], resumed["id"], resumed["attempt_id"], "COMPLETED", {"answer": "waiting"})
    judge_once(db, settings, JudgeGateway({"verdict": "held", "explanation": "无先例"}))
    assert judge_view(task["id"])["verdict"] == "held"  # 裁判补写的 @user 提问也能对上运行
    cards.action(task["id"], "answer", body="蓝色")
    assert judge_view(task["id"]) is None  # 转交用户的问题回答后不再显示
