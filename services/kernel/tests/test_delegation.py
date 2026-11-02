"""Bot 协作 C2：委派、等待、WAITING 自动唤醒与硬上限。"""

import asyncio
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_collab import ENGINES, board, call, finish, new_goal

from niucai.api.app import create_app
from niucai.control import collab
from niucai.control.cards import BOARD_TOOLS, CardManager, wake_waiters
from niucai.control.orchestrator import Orchestrator
from niucai.control.tasks import Conflict
from niucai.storage.db import Card, CardEvent, Conversation, OrchestratorRun, now

ALL = {"can_delegate": True, "delegate_to": ["*"]}


def bot(cards, node, name, engine="claude", **extra):
    return cards.create_bot({"name": name, "node_id": node["id"], "engine": engine, **extra})


def card(cards, title, **extra):
    return cards.create_card({"title": title, "goal": "do it", **extra})


def status(db, ident):
    with db.sessions() as s:
        return s.get(Card, ident).status


def start(setup, policy=ALL, goal_mode=False, slots=3, **kwargs):
    """A（可委派）领取了一张卡：返回 (环境, A 的 Attempt, 父卡 ID)。"""
    db, settings, remote, cards, node = board(setup, slots=slots)
    a = bot(cards, node, "A", collab_policy=policy, **kwargs)
    goal = new_goal(db)
    if goal_mode:
        with db.sessions.begin() as s:
            s.get(Conversation, goal).settings = {"mode": "goal"}
    parent = card(cards, "parent", assignee_bot_id=a["id"], conversation_id=goal)
    claimed = remote.claim(node["id"])
    remote.event(node["id"], claimed["id"], claimed["attempt_id"], 1, "session", {"session_id": "sess-A"})
    return (db, settings, remote, cards, node), claimed, parent["id"], a, goal


def complete(env, claimed, summary="done"):
    db, settings, remote, cards, node = env
    call(cards, node, claimed, "card_submit", summary=summary)
    finish(remote, node, claimed)


def test_delegate_and_wait_then_resume_with_result(setup):
    env, claimed, parent, a, goal = start(setup)
    db, settings, remote, cards, node = env
    b = bot(cards, node, "B", engine="codex", permission_profile="read")

    result = call(cards, node, claimed, "card_delegate", to="B", title="research", goal="find it", wait=True)
    child = result["card_id"]
    assert result["status"] == "READY" and result["ok"]
    with db.sessions() as s:
        row = s.get(Card, child)
        assert (row.parent_card_id, row.created_by, row.depth) == (parent, f"bot:{a['id']}", 1)
        assert row.conversation_id == goal and row.assignee_bot_id == b["id"]
        kinds = [e.kind for e in s.scalars(select(CardEvent).where(CardEvent.card_id == child))]
        assert "delegate" in kinds
    finish(remote, node, claimed)  # A 的 Attempt 在 card_wait 之后结束
    assert status(db, parent) == "WAITING"
    detail = cards.get(parent)
    assert detail["waiting_on"]["cards"] == [child] and detail["current_job"] is None
    assert cards.get(child)["initiator_name"] == "A"

    b_job = remote.claim(node["id"])
    assert b_job["card_id"] == child and b_job["config_snapshot"]["permission_profile"] == "read"
    put = call(cards, node, b_job, "artifact_put", name="r.txt", text="42")
    complete(env, b_job, "found 42")
    assert status(db, child) == "DONE" and status(db, parent) == "READY"

    resumed = remote.claim(node["id"])
    assert resumed["card_id"] == parent
    assert resumed["config_snapshot"]["resume"] == {"job_id": claimed["id"], "session_id": "sess-A"}
    prompt = resumed["prompt"]
    assert "来自 Bot B 的结果" in prompt and "found 42" in prompt
    assert put["artifact_id"] in prompt and "不是用户指令" in prompt
    assert cards.get(parent)["waiting_on"] is None


@pytest.mark.parametrize("how", ["failed", "cancelled"])
def test_child_failed_or_cancelled_also_wakes_parent(setup, how):
    env, claimed, parent, a, goal = start(setup)
    db, settings, remote, cards, node = env
    bot(cards, node, "B", engine="codex")
    child = call(cards, node, claimed, "card_delegate", to="B", title="t", goal="g", wait=True)["card_id"]
    finish(remote, node, claimed)
    assert status(db, parent) == "WAITING"
    if how == "cancelled":
        cards.action(child, "cancel")
    else:
        with db.sessions.begin() as s:
            s.get(Card, child).budget = {"max_attempts": 1}
        job = remote.claim(node["id"])
        remote.finish(node["id"], job["id"], job["attempt_id"], "FAILED", {"error": "boom"})
    assert status(db, child) == how.upper()
    assert status(db, parent) == "READY"
    prompt = remote.claim(node["id"])["prompt"]
    assert how.upper() in prompt


@pytest.mark.parametrize("mode", ["all", "any"])
def test_wait_mode_all_vs_any(setup, mode):
    env, claimed, parent, a, goal = start(setup)
    db, settings, remote, cards, node = env
    bot(cards, node, "B", engine="codex")
    first = call(cards, node, claimed, "card_delegate", to="B", title="one", goal="g")["card_id"]
    second = call(cards, node, claimed, "card_delegate", to="B", title="two", goal="g")["card_id"]
    call(cards, node, claimed, "card_wait", card_ids=[first, second], mode=mode)
    finish(remote, node, claimed)
    assert status(db, parent) == "WAITING"

    job = remote.claim(node["id"])
    assert job["card_id"] == first
    complete(env, job)
    assert status(db, parent) == ("WAITING" if mode == "all" else "READY")
    if mode == "all":
        job = remote.claim(node["id"])
        assert job["card_id"] == second
        complete(env, job)
    assert status(db, parent) == "READY"
    with db.sessions() as s:
        woken = [
            e
            for e in s.scalars(
                select(CardEvent).where(CardEvent.card_id == parent, CardEvent.kind == "status")
            )
            if e.data.get("to") == "READY" and e.data.get("woken_by")
        ]
    assert len(woken) == 1  # 只唤醒一次


def test_double_wake_is_idempotent_and_early_finish_is_not_lost(setup):
    env, claimed, parent, a, goal = start(setup)
    db, settings, remote, cards, node = env
    bot(cards, node, "B", engine="codex")
    child = call(cards, node, claimed, "card_delegate", to="B", title="t", goal="g")["card_id"]
    call(cards, node, claimed, "card_wait", card_ids=[child])
    # 被等待的卡片在父卡的 Attempt 收尾之前就结束了：不能丢这次唤醒。
    job = remote.claim(node["id"])
    complete(env, job)
    assert status(db, parent) == "RUNNING"
    finish(remote, node, claimed)
    assert status(db, parent) == "READY"
    with db.sessions.begin() as s:
        wake_waiters(s, s.get(Card, child))  # 重复触发：没有 WAITING 的卡，不会再产生事件
    with db.sessions() as s:
        ready = [
            e
            for e in s.scalars(
                select(CardEvent).where(CardEvent.card_id == parent, CardEvent.kind == "status")
            )
            if e.data.get("to") == "READY"
        ]
    assert len(ready) == 2  # 创建时 DRAFT→READY 一次，等待结束一次
    assert remote.claim(node["id"])["card_id"] == parent


def test_wait_returns_immediately_when_already_satisfied(setup):
    env, claimed, parent, a, goal = start(setup)
    db, settings, remote, cards, node = env
    bot(cards, node, "B", engine="codex")
    child = call(cards, node, claimed, "card_delegate", to="B", title="t", goal="g")["card_id"]
    job = remote.claim(node["id"])
    complete(env, job, "quick")
    result = call(cards, node, claimed, "card_wait", card_ids=[child])
    assert result["ready"] and result["cards"][0]["summary"] == "quick"
    assert status(db, parent) == "RUNNING"  # Attempt 不结束


def test_wait_timeout_wakes_with_note_and_notifies_orchestrator(setup):
    env, claimed, parent, a, goal = start(setup, goal_mode=True)
    db, settings, remote, cards, node = env
    bot(cards, node, "B", engine="codex")
    child = call(cards, node, claimed, "card_delegate", to="B", title="t", goal="g")["card_id"]
    call(cards, node, claimed, "card_wait", card_ids=[child], timeout=60)
    finish(remote, node, claimed)
    assert status(db, parent) == "WAITING"
    with db.sessions.begin() as s:
        row = s.get(Card, parent)
        row.waiting_on = {**row.waiting_on, "until": (now() - timedelta(seconds=1)).isoformat()}

    resumed = remote.claim(node["id"])  # 领取入口的 expire 先唤醒超时的卡
    assert resumed["card_id"] == parent and "等待已超时" in resumed["prompt"]
    assert status(db, child) == "READY"
    with db.sessions() as s:
        kinds = [t["kind"] for run in s.scalars(select(OrchestratorRun)) for t in run.triggers]
    assert "delegated" in kinds and "wait_timeout" in kinds


@pytest.mark.parametrize("bad", [0, 14401, "x", True])
def test_wait_timeout_is_validated(setup, bad):
    env, claimed, parent, a, goal = start(setup)
    db, settings, remote, cards, node = env
    bot(cards, node, "B", engine="codex")
    child = call(cards, node, claimed, "card_delegate", to="B", title="t", goal="g")["card_id"]
    with pytest.raises(ValueError):
        call(cards, node, claimed, "card_wait", card_ids=[child], timeout=bad)
    with pytest.raises(ValueError):
        call(cards, node, claimed, "card_wait", card_ids=[child], mode="some")
    with pytest.raises(Conflict):
        call(cards, node, claimed, "card_wait", card_ids=[parent])


def test_cross_goal_wait_is_rejected(setup):
    env, claimed, parent, a, goal = start(setup)
    db, settings, remote, cards, node = env
    other = card(cards, "elsewhere", conversation_id=new_goal(db), draft=True)
    with pytest.raises(PermissionError):
        call(cards, node, claimed, "card_wait", card_ids=[other["id"]])


def test_mutual_wait_is_rejected(setup):
    env, claimed, parent, a, goal = start(setup)
    db, settings, remote, cards, node = env
    bot(cards, node, "B", engine="codex", collab_policy=ALL)
    child = call(cards, node, claimed, "card_delegate", to="B", title="t", goal="g", wait=True)["card_id"]
    finish(remote, node, claimed)
    b_job = remote.claim(node["id"])
    assert b_job["card_id"] == child
    # B 反过来等 A（卡片层面成环），或再委派给 A 并等待（Bot 层面成环）。
    with pytest.raises(Conflict, match="环形等待"):
        call(cards, node, b_job, "card_wait", card_ids=[parent])
    with pytest.raises(Conflict, match="环形等待"):
        call(cards, node, b_job, "card_delegate", to="A", title="back", goal="g", wait=True)


def test_delegation_depth_limit(setup):
    env, claimed, parent, a, goal = start(setup, slots=4)
    db, settings, remote, cards, node = env
    for name in "BCD":
        bot(cards, node, name, engine="codex", collab_policy=ALL)
    c1 = call(cards, node, claimed, "card_delegate", to="B", title="1", goal="g")["card_id"]
    job_b = remote.claim(node["id"])
    c2 = call(cards, node, job_b, "card_delegate", to="C", title="2", goal="g")["card_id"]
    job_c = remote.claim(node["id"])
    assert job_c["card_id"] == c2
    with db.sessions() as s:
        assert (s.get(Card, c1).depth, s.get(Card, c2).depth) == (1, 2)
    with pytest.raises(Conflict, match="深度"):
        call(cards, node, job_c, "card_delegate", to="D", title="3", goal="g")
    settings.delegation_max_depth = 3
    assert call(cards, node, job_c, "card_delegate", to="D", title="3", goal="g")["status"] == "READY"


def test_goal_budget_rejects_delegation_and_kernel_cards(setup):
    env, claimed, parent, a, goal = start(setup)
    db, settings, remote, cards, node = env
    bot(cards, node, "B", engine="codex")
    settings.goal_max_cards = 2
    call(cards, node, claimed, "card_delegate", to="B", title="1", goal="g")
    with pytest.raises(Conflict, match="张卡片"):
        call(cards, node, claimed, "card_delegate", to="B", title="2", goal="g")
    orchestrator = Orchestrator(db, settings, None)
    with pytest.raises(Conflict, match="张卡片"):
        orchestrator.tool_card_create(goal, {"title": "kernel card"})
    settings.goal_max_cards = 50
    settings.goal_max_attempts = 1  # A 的这次 Attempt 已经用掉额度
    with pytest.raises(Conflict, match="Attempt"):
        call(cards, node, claimed, "card_delegate", to="B", title="3", goal="g")
    with pytest.raises(Conflict, match="Attempt"):
        orchestrator.tool_card_create(goal, {"title": "kernel card"})


def test_policy_rejections(setup):
    env, claimed, parent, a, goal = start(setup, policy={})  # 默认不能委派
    db, settings, remote, cards, node = env
    b = bot(cards, node, "B", engine="codex", collab_policy={"accept_from": ["Z"]})
    bot(cards, node, "C", engine="codex")
    with pytest.raises(Conflict, match="can_delegate"):
        call(cards, node, claimed, "card_delegate", to="B", title="t", goal="g")
    cards.update_bot(a["id"], {"collab_policy": {"can_delegate": True, "delegate_to": ["B"]}})
    with pytest.raises(Conflict, match="delegate_to"):
        call(cards, node, claimed, "card_delegate", to="C", title="t", goal="g")
    with pytest.raises(Conflict, match="accept_from"):
        call(cards, node, claimed, "card_delegate", to="B", title="t", goal="g")
    with pytest.raises(Conflict, match="自己"):
        call(cards, node, claimed, "card_delegate", to="A", title="t", goal="g")
    with pytest.raises(ValueError, match="unknown"):
        call(cards, node, claimed, "card_delegate", to="nobody", title="t", goal="g")
    cards.update_bot(b["id"], {"collab_policy": {"accept_from": ["tag:lead", "A"]}})
    assert call(cards, node, claimed, "card_delegate", to="B", title="t", goal="g")["status"] == "READY"


def test_tag_delegation_respects_policy(setup):
    env, claimed, parent, a, goal = start(
        setup, policy={"can_delegate": True, "delegate_to": ["tag:browser"]}
    )
    db, settings, remote, cards, node = env
    bot(cards, node, "plain", engine="codex", tags=["code"])
    web = bot(cards, node, "web", engine="codex", tags=["browser"])
    with pytest.raises(Conflict, match="没有符合"):
        call(cards, node, claimed, "card_delegate", tags=["code"], title="t", goal="g")
    child = call(cards, node, claimed, "card_delegate", tags=["browser"], title="t", goal="g")["card_id"]
    job = remote.claim(node["id"])
    assert job["card_id"] == child and job["bot_id"] == web["id"]


def test_collab_policy_http_api_and_config_version(setup):
    db, settings, *_ = setup
    settings.remote_pi_enabled = True
    auth = {"Authorization": f"Bearer {settings.api_token.get_secret_value()}"}
    api = TestClient(create_app(settings, db))
    node = api.post("/api/remote/nodes", json={"name": "n1"}, headers=auth).json()
    created = api.post(
        "/api/bots", json={"name": "c1", "node_id": node["id"], "engine": "claude"}, headers=auth
    ).json()
    assert created["collab_policy"] == collab.POLICY_DEFAULTS
    url = f"/api/bots/{created['id']}"
    patched = api.patch(
        url, json={"collab_policy": {"can_delegate": True, "delegate_to": ["tag:x", "d1"]}}, headers=auth
    )
    assert patched.status_code == 200
    policy = patched.json()["collab_policy"]
    assert policy["can_delegate"] is True and policy["delegate_to"] == ["tag:x", "d1"]
    assert policy["accept_from"] == ["*"]  # 没传的字段保持
    assert patched.json()["config_version"] == created["config_version"]
    for bad in (
        {"nope": 1},
        {"can_delegate": "yes"},
        {"delegate_to": ["has space"]},
        {"delegate_to": "d1"},
    ):
        assert api.patch(url, json={"collab_policy": bad}, headers=auth).status_code == 422, bad
    assert api.get("/api/bots", headers=auth).json()[0]["collab_policy"] == policy


def test_approval_rules_and_receiver_profile(setup):
    env, claimed, parent, a, goal = start(setup)
    db, settings, remote, cards, node = env
    local = remote.create_node("this-pc", resource_type="windows_local")
    remote.node_heartbeat(local["id"], {"available": True, "platform": "windows", "executors": ENGINES})
    guarded = bot(cards, node, "guarded", engine="codex", requires_approval=True, permission_profile="read")
    bot(
        cards,
        node,
        "trusted",
        engine="codex",
        requires_approval=True,
        collab_policy={"auto_approve_from": ["A"]},
    )
    bot(cards, node, "plain", engine="codex")
    bot(cards, local, "desk")  # Windows 本机：read + 需审批
    bot(cards, local, "desk_open", requires_approval=False, permission_profile="workspace")
    bot(cards, local, "desk_auto", collab_policy={"auto_approve_from": ["tag:lead"]})

    def delegate(to):
        return call(cards, node, claimed, "card_delegate", to=to, title=to, goal="g")

    assert delegate("guarded")["status"] == "WAITING_APPROVAL"
    assert delegate("trusted")["status"] == "READY"
    assert delegate("plain")["status"] == "READY"
    assert delegate("desk")["status"] == "WAITING_APPROVAL"
    assert delegate("desk_open")["status"] == "WAITING_APPROVAL"  # Windows 节点默认就要批准
    cards.update_bot(
        cards.bots()[-1]["id"], {"collab_policy": {"auto_approve_from": ["A"]}}
    )  # desk_auto 改为信任 A
    assert delegate("desk_auto")["status"] == "READY"
    with pytest.raises(ValueError, match="permission_profile"):
        call(
            cards, node, claimed, "card_delegate", to="plain", title="x", goal="g", permission_profile="full"
        )

    # 批准后正常领取，并且只用接收方自己的权限档位。
    pending = next(c for c in cards.board()["cards"] if c["assignee_name"] == "guarded")
    cards.action(pending["id"], "approve")
    job = next(j for j in iter(lambda: remote.claim(node["id"]), None) if j["bot_id"] == guarded["id"])
    assert job["config_snapshot"]["permission_profile"] == "read"


def test_waiting_keeps_shared_but_not_exclusive_claims(setup):
    env, claimed, parent, a, goal = start(setup)
    db, settings, remote, cards, node = env
    bot(cards, node, "B", engine="codex")
    other = bot(cards, node, "O", engine="codex")
    call(cards, node, claimed, "card_claim", resources=[{"resource": "browser:vps", "mode": "exclusive"}])
    child = call(cards, node, claimed, "card_delegate", to="B", title="t", goal="g")["card_id"]
    rival = card(
        cards, "rival", assignee_bot_id=other["id"], claims=[{"resource": "browser:vps", "mode": "exclusive"}]
    )
    call(cards, node, claimed, "card_wait", card_ids=[child])
    finish(remote, node, claimed)
    assert status(db, parent) == "WAITING"
    with db.sessions() as s:
        assert s.get(Card, parent).claims == [{"resource": "browser:vps", "mode": "exclusive"}]
        conflicts, hints = collab.claim_conflicts(
            s, s.get(Card, rival["id"]), [{"resource": "browser:vps", "mode": "exclusive"}]
        )
    assert conflicts == [] and len(hints) == 1  # 等待中不再独占，但声明仍可见


def test_cancel_parent_cancels_unstarted_children_only(setup):
    env, claimed, parent, a, goal = start(setup)
    db, settings, remote, cards, node = env
    bot(cards, node, "B", engine="codex")
    running = call(cards, node, claimed, "card_delegate", to="B", title="r", goal="g")["card_id"]
    queued = call(cards, node, claimed, "card_delegate", to="B", title="q", goal="g")["card_id"]
    call(cards, node, claimed, "card_wait", card_ids=[running, queued])
    finish(remote, node, claimed)
    job = remote.claim(node["id"])  # (B, Goal) 同时只跑一张：queued 仍未开始
    assert job["card_id"] == running and status(db, queued) == "READY"

    cards.action(parent, "cancel")  # 用户可以取消 WAITING 的卡
    assert status(db, parent) == "CANCELLED" and cards.get(parent)["waiting_on"] is None
    assert status(db, queued) == "CANCELLED"
    assert status(db, running) == "RUNNING"
    complete(env, job, "kept result")
    assert status(db, running) == "DONE" and status(db, parent) == "CANCELLED"
    assert cards.get(running)["result"]["summary"] == "kept result"


def test_board_tools_include_delegate_and_wait():
    assert BOARD_TOOLS[-2:] == ("card_delegate", "card_wait")
    assert CardManager.tool_card_delegate and CardManager.tool_card_wait
    assert asyncio  # 保持导入以便后续并发用例扩展
