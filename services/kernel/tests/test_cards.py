import base64
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from niucai.api.app import create_app
from niucai.control.cards import CardManager
from niucai.control.remote import RemoteManager
from niucai.control.tasks import Conflict
from niucai.storage.db import Card, McpServer, RemoteJob, Skill, now

ENGINES = {"pi": {"available": True}, "codex": {"available": True}, "claude": {"available": True}}


def board(setup, slots=1):
    db, settings, *_ = setup
    settings.remote_pi_enabled = True
    remote, cards = RemoteManager(db, settings), CardManager(db, settings)
    node = remote.create_node("vps-1")
    if slots != 1:
        remote.update_node(node["id"], {"slots": slots})
    remote.node_heartbeat(node["id"], {"available": True, "protocol_version": 3, "executors": ENGINES})
    return db, settings, remote, cards, node


def bot(cards, node, name="coder", engine="claude", **extra):
    return cards.create_bot({"name": name, "node_id": node["id"], "engine": engine, **extra})


def card(cards, title="task", **extra):
    return cards.create_card({"title": title, "goal": "do it", **extra})


def status(db, card_id):
    with db.sessions() as s:
        return s.get(Card, card_id).status


def test_bot_crud_validation_and_config_version(setup):
    db, settings, remote, cards, node = board(setup)
    coder = bot(cards, node, tags=["py"])
    assert coder["config_version"] == 1 and coder["concurrency"] == 1
    with pytest.raises(Conflict):
        bot(cards, node)
    with pytest.raises(ValueError):
        bot(cards, node, name="has space")
    with pytest.raises(ValueError):
        bot(cards, node, name="kernel")
    with pytest.raises(ValueError):
        bot(cards, node, name="pi-bot", engine="pi", mcp_servers=["fs"])
    renamed = cards.update_bot(coder["id"], {"role": "reviewer"})
    assert renamed["config_version"] == 2
    assert cards.update_bot(coder["id"], {"enabled": False})["config_version"] == 2
    assert cards.bots()[0]["node_name"] == "vps-1"
    assert remote_topology_bots(setup) == [coder["id"]]
    card(cards, assignee_bot_id=coder["id"])
    with pytest.raises(Conflict):
        cards.delete_bot(coder["id"])
    spare = bot(cards, node, name="spare")
    assert cards.delete_bot(spare["id"]) == {"deleted": spare["id"]}


def remote_topology_bots(setup):
    db, settings, *_ = setup
    from niucai.control.pairing import PairingManager

    [entry] = PairingManager(db, settings).topology()["nodes"]
    return [b["id"] for b in entry["bots"]]


def test_claim_respects_slots_bot_concurrency_tags_and_approval(setup):
    db, settings, remote, cards, node = board(setup, slots=2)
    coder = bot(cards, node, tags=["py"])
    gated = bot(cards, node, name="gated", requires_approval=True)
    first = card(cards, "first", required_tags=["py"])
    second = card(cards, "second")
    untagged = card(cards, "rust", required_tags=["rust"])
    approval = card(cards, "needs ok", assignee_bot_id=gated["id"])
    assert approval["status"] == "WAITING_APPROVAL"

    claimed = remote.claim(node["id"])
    assert claimed["card_id"] == first["id"] and claimed["bot_id"] == coder["id"]
    assert claimed["board_token"] and claimed["artifact_transport"] is False
    snapshot = claimed["config_snapshot"]
    assert snapshot["workspace"] == f"card-{first['id']}" and snapshot["bot"]["name"] == "coder"
    # coder 并发 1 已满，gated 不进公共池，rust 卡没有匹配标签。
    assert remote.claim(node["id"]) is None

    cards.action(approval["id"], "approve")
    assert remote.claim(node["id"])["card_id"] == approval["id"]
    # 节点 2 个槽位已满。
    cards.update_bot(coder["id"], {"concurrency": 4})
    assert remote.claim(node["id"]) is None
    assert status(db, second["id"]) == "READY" and status(db, untagged["id"]) == "READY"
    assert remote.nodes()[0]["busy"] == 2


def test_dependencies_gate_claim_and_pass_inline_inputs(setup):
    db, settings, remote, cards, node = board(setup)
    coder = bot(cards, node)
    design = card(cards, "design")
    build = card(cards, "build", depends_on=[design["id"]])
    with pytest.raises(ValueError):
        cards.update_card(design["id"], 2, {"depends_on": [build["id"]]})

    claimed = remote.claim(node["id"])
    assert claimed["card_id"] == design["id"]
    files = [{"path": "spec.md", "base64": base64.b64encode(b"# spec").decode()}]
    remote.finish(
        node["id"], claimed["id"], claimed["attempt_id"], "COMPLETED", {"answer": "spec done", "files": files}
    )
    assert status(db, design["id"]) == "DONE"

    next_claim = remote.claim(node["id"])
    assert next_claim["card_id"] == build["id"] and next_claim["bot_id"] == coder["id"]
    [inputs] = next_claim["config_snapshot"]["card_inputs"]
    assert inputs["summary"] == "spec done" and inputs["files"] == files
    assert "spec done" in next_claim["prompt"]


def test_board_tools_block_answer_resume_and_submit(setup):
    db, settings, remote, cards, node = board(setup)
    coder = bot(cards, node)
    reviewer = bot(cards, node, name="reviewer", engine="codex")
    task = card(cards, "feature")
    claimed = remote.claim(node["id"])
    job, token = claimed["id"], claimed["board_token"]
    with pytest.raises(PermissionError):
        cards.board_call(node["id"], job, "wrong", "card_get", {})
    with pytest.raises(ValueError):
        cards.board_call(node["id"], job, token, "shell", {})

    assert cards.board_call(node["id"], job, token, "board_view", {})["cards"][0]["id"] == task["id"]
    args = {"text": "halfway", "percent": 50, "idempotency_key": "p1"}
    cards.board_call(node["id"], job, token, "card_progress", args)
    cards.board_call(node["id"], job, token, "card_progress", args)  # 幂等重放不重复记事件
    cards.board_call(node["id"], job, token, "card_comment", {"body": "@reviewer 请看接口"})
    cards.board_call(node["id"], job, token, "card_propose", {"title": "add tests"})
    remote.event(node["id"], job, claimed["attempt_id"], 1, "session", {"session_id": "sess-1"})
    cards.board_call(node["id"], job, token, "card_block", {"question": "用哪个数据库？"})
    remote.finish(node["id"], job, claimed["attempt_id"], "COMPLETED", {"answer": "waiting"})
    detail = cards.get(task["id"])
    assert detail["status"] == "BLOCKED"
    kinds = [e["kind"] for e in detail["events"]]
    assert kinds.count("progress") == 1 and "proposal" in kinds and "question" in kinds
    with pytest.raises(Conflict):
        cards.board_call(node["id"], job, token, "card_progress", {"text": "late"})

    cards.action(task["id"], "answer", body="PostgreSQL")
    resumed = remote.claim(node["id"])
    assert resumed["config_snapshot"]["resume"] == {"job_id": job, "session_id": "sess-1"}
    assert "PostgreSQL" in resumed["prompt"]
    # reviewer 的收件箱能看到 @ 提及（以 reviewer 的 Attempt 调用需另一张卡，这里直接读事件）。
    mention = next(e for e in detail["events"] if e["kind"] == "comment")
    assert mention["mentions"] == ["reviewer"] and reviewer["name"] == "reviewer"

    token2 = resumed["board_token"]
    cards.board_call(node["id"], resumed["id"], token2, "card_submit", {"summary": "done with pg"})
    remote.finish(node["id"], resumed["id"], resumed["attempt_id"], "COMPLETED", {"answer": "x"})
    final = cards.get(task["id"])
    assert final["status"] == "DONE" and final["result"]["summary"] == "done with pg"
    assert [a["bot_id"] for a in final["runs"]] == [coder["id"], coder["id"]]


def test_review_reject_and_failure_budget(setup):
    db, settings, remote, cards, node = board(setup)
    bot(cards, node, engine="pi")
    reviewed = card(cards, "doc", review="llm", budget={"max_attempts": 2})
    claimed = remote.claim(node["id"])
    remote.finish(node["id"], claimed["id"], claimed["attempt_id"], "COMPLETED", {"answer": "draft"})
    assert status(db, reviewed["id"]) == "REVIEW"
    with pytest.raises(ValueError):
        cards.action(reviewed["id"], "reject")
    cards.action(reviewed["id"], "reject", body="补充示例")
    retry = remote.claim(node["id"])
    assert "补充示例" in retry["prompt"] and "resume" not in retry["config_snapshot"]  # Pi 不续接
    remote.finish(node["id"], retry["id"], retry["attempt_id"], "FAILED", {"error": "boom"})
    detail = cards.get(reviewed["id"])
    assert detail["status"] == "FAILED" and "boom" in detail["result"]["error"]
    cards.action(reviewed["id"], "reopen")
    assert status(db, reviewed["id"]) == "READY"


def test_cancel_stops_attempt_and_finish_does_not_reopen(setup):
    db, settings, remote, cards, node = board(setup)
    bot(cards, node)
    task = card(cards)
    claimed = remote.claim(node["id"])
    cards.action(task["id"], "cancel")
    beat = remote.heartbeat(node["id"], claimed["id"], claimed["attempt_id"])
    assert beat["stop"] is True
    remote.finish(node["id"], claimed["id"], claimed["attempt_id"], "COMPLETED", {"answer": "late"})
    assert status(db, task["id"]) == "CANCELLED"
    with db.sessions() as s:
        assert s.get(RemoteJob, claimed["id"]).status == "CANCELLED"


def test_lost_attempt_revives_within_grace_and_requeues_after(setup):
    db, settings, remote, cards, node = board(setup)
    bot(cards, node)
    task = card(cards)
    claimed = remote.claim(node["id"])
    with db.sessions.begin() as s:
        s.get(RemoteJob, claimed["id"]).lease_until = now() - timedelta(seconds=5)
    remote.node_heartbeat(node["id"], {"available": True, "protocol_version": 3, "executors": ENGINES})
    with db.sessions() as s:
        assert s.get(RemoteJob, claimed["id"]).status == "LOST"
    assert status(db, task["id"]) == "RUNNING"
    # 断网恢复：同一 Attempt 的事件续传，不重复。
    assert remote.event(node["id"], claimed["id"], claimed["attempt_id"], 1, "text", {"text": "a"})[
        "accepted"
    ]
    assert not remote.event(node["id"], claimed["id"], claimed["attempt_id"], 1, "text", {"text": "a"})[
        "accepted"
    ]
    with db.sessions() as s:
        assert s.get(RemoteJob, claimed["id"]).status == "RUNNING"

    with db.sessions.begin() as s:
        s.get(RemoteJob, claimed["id"]).lease_until = now() - timedelta(
            seconds=settings.card_lost_grace_seconds + 5
        )
    remote.node_heartbeat(node["id"], {"available": True, "protocol_version": 3, "executors": ENGINES})
    assert status(db, task["id"]) == "READY"
    # LOST 仍占槽位：确认退出前不再领取。
    assert remote.claim(node["id"]) is None
    remote.finish(node["id"], claimed["id"], claimed["attempt_id"], "FAILED", {"error": "gone"})
    again = remote.claim(node["id"])
    assert again["card_id"] == task["id"] and again["id"] != claimed["id"]


def test_board_http_api(setup):
    db, settings, *_ = setup
    settings.remote_pi_enabled = True
    auth = {"Authorization": f"Bearer {settings.api_token.get_secret_value()}"}
    api = TestClient(create_app(settings, db))
    node = api.post("/api/remote/nodes", json={"name": "n1"}, headers=auth).json()
    node_auth = {"Authorization": f"Bearer {node['token']}"}
    beat = {"available": True, "protocol_version": 3, "executors": {"claude": {"available": True}}}
    assert (
        api.post(f"/api/remote/runner/{node['id']}/heartbeat", json=beat, headers=node_auth).json()["slots"]
        == 1
    )
    assert api.get("/api/board").status_code == 401
    created = api.post(
        "/api/bots", json={"name": "c1", "node_id": node["id"], "engine": "claude"}, headers=auth
    )
    assert created.status_code == 201
    draft = api.post("/api/cards", json={"title": "t", "draft": True}, headers=auth).json()
    assert draft["status"] == "DRAFT"
    stale = api.patch(f"/api/cards/{draft['id']}", json={"expected_version": 9, "title": "x"}, headers=auth)
    assert stale.status_code == 409
    assert api.post(f"/api/cards/{draft['id']}/ready", json={}, headers=auth).json()["status"] == "READY"
    assert (
        api.post(f"/api/cards/{draft['id']}/comments", json={"body": "@c1 hi"}, headers=auth).status_code
        == 201
    )
    view = api.get("/api/board", headers=auth).json()
    assert view["cards"][0]["id"] == draft["id"] and view["bots"][0]["name"] == "c1"

    claimed = api.post(f"/api/remote/runner/{node['id']}/claim?wait=1", headers=node_auth).json()
    assert claimed["card_id"] == draft["id"]
    url = f"/api/remote/runner/{node['id']}/jobs/{claimed['id']}/board"
    assert api.post(f"{url}/inbox", json={"args": {}}, headers=node_auth).status_code == 422
    headers = {**node_auth, "X-Board-Token": claimed["board_token"]}
    inbox = api.post(f"{url}/inbox", json={"args": {}}, headers=headers).json()
    assert inbox["messages"][0]["body"] == "@c1 hi"
    assert api.post(f"{url}/inbox", json={"args": {}}, headers=headers).json()["messages"] == []
    bad = {**node_auth, "X-Board-Token": "nope"}
    assert api.post(f"{url}/card_get", json={"args": {}}, headers=bad).status_code == 403
    # 没有可领取卡片时长轮询返回 null。
    assert api.post(f"/api/remote/runner/{node['id']}/claim?wait=1", headers=node_auth).json() is None


def test_card_workdir_requires_assignee_and_reaches_snapshot(setup):
    db, settings, remote, cards, node = board(setup, slots=2)
    coder = bot(cards, node, concurrency=2)
    pooled = card(cards, "pooled", workdir="D:/Code/app")
    assert remote.claim(node["id"]) is None  # 未指派的本机目录卡片不进公共池
    cards.update_card(pooled["id"], None, {"assignee_bot_id": coder["id"]})
    claimed = remote.claim(node["id"])
    assert claimed["card_id"] == pooled["id"] and claimed["config_snapshot"]["workdir"] == "D:/Code/app"
    card(cards, "plain")
    assert "workdir" not in remote.claim(node["id"])["config_snapshot"]


def test_bot_skills_and_mcp_servers_reach_snapshot(setup):
    db, settings, remote, cards, node = board(setup)
    with db.sessions() as s:
        s.add(Skill(name="review", description="代码审查", content="先读 diff"))
        s.add(Skill(name="off", content="x", enabled=0))
        s.add(
            McpServer(
                name="fs",
                transport="stdio",
                config={"command": "npx", "args": ["fs"], "env": {"TOKEN": "env:FS_TOKEN"}},
            )
        )
        s.commit()
    coder = bot(cards, node, skills=["review", "off", "ghost"], mcp_servers=["fs"])
    view = next(b for b in cards.bots() if b["id"] == coder["id"])
    assert view["missing_assets"] == ["skill:ghost", "skill:off"] and view["node_env"] == ["FS_TOKEN"]
    card(cards)
    snapshot = remote.claim(node["id"])["config_snapshot"]
    assert [x["name"] for x in snapshot["skills"]] == ["review"]
    assert snapshot["skills"][0]["content"] == "先读 diff"
    assert snapshot["mcp_servers"] == [
        {
            "name": "fs",
            "transport": "stdio",
            "config": {"command": "npx", "args": ["fs"], "env": {"TOKEN": "env:FS_TOKEN"}},
        }
    ]
