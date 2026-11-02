import base64

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from niucai.api.app import create_app
from niucai.control import collab
from niucai.control.cards import CardManager
from niucai.control.remote import RemoteManager
from niucai.control.tasks import Conflict
from niucai.storage.db import Card, CardEvent

ENGINES = {"pi": {"available": True}, "codex": {"available": True}, "claude": {"available": True}}
PROBE = {
    "os": "Windows 11",
    "engines": {"claude": {"available": True, "version": "2.1.0"}, "codex": {"available": True}},
    "browser": {"available": True, "cdp": True},
    "repos": [{"name": "niucai", "remote": "https://user:secret@example.com/org/niucai.git"}],
    "cli": ["git", "node"],
}


def board(setup, slots=2):
    db, settings, *_ = setup
    settings.remote_pi_enabled = True
    remote, cards = RemoteManager(db, settings), CardManager(db, settings)
    node = remote.create_node("vps-1")
    remote.update_node(node["id"], {"slots": slots})
    remote.node_heartbeat(
        node["id"],
        {"available": True, "protocol_version": 3, "executors": ENGINES, "environment": PROBE},
    )
    return db, settings, remote, cards, node


def bot(cards, node, name, engine="claude", **extra):
    return cards.create_bot({"name": name, "node_id": node["id"], "engine": engine, **extra})


def card(cards, title, **extra):
    return cards.create_card({"title": title, "goal": "do it", **extra})


def call(cards, node, claimed, tool, **args):
    return cards.board_call(node["id"], claimed["id"], claimed["board_token"], tool, args)


def finish(remote, node, claimed):
    remote.finish(node["id"], claimed["id"], claimed["attempt_id"], "COMPLETED", {"answer": "ok"})


def test_bots_list_shows_colleagues_with_environment_and_state(setup):
    db, settings, remote, cards, node = board(setup)
    coder = bot(cards, node, "coder")
    browser = bot(cards, node, "browser", engine="codex")
    cards.update_bot(
        browser["id"],
        {"environment": {"logged_in_sites": ["github.com"], "resources": ["内网 VPN"], "notes": "夜间慢"}},
    )
    card(cards, "task", assignee_bot_id=coder["id"])
    claimed = remote.claim(node["id"])

    roster = {b["name"]: b for b in call(cards, node, claimed, "bots_list")["bots"]}

    assert set(roster) == {"coder", "browser"}
    assert roster["coder"]["self"] and roster["coder"]["state"] == "full"
    assert roster["browser"]["state"] == "idle" and roster["browser"]["online"]
    env = roster["browser"]["environment"]
    assert env["probed"]["os"] == "Windows 11"
    assert env["probed"]["engine"] == {"name": "codex", "available": True, "version": ""}
    assert env["probed"]["browser"] == {"available": True, "cdp": True}
    assert env["probed"]["repos"] == [{"name": "niucai", "remote": "https://example.com/org/niucai.git"}]
    assert env["manual"]["logged_in_sites"] == ["github.com"]
    assert env["manual"]["notes"] == "夜间慢"
    assert "instructions" not in roster["browser"]


def test_environment_manual_edit_keeps_probe_and_validates(setup):
    db, settings, remote, cards, node = board(setup)
    helper = bot(cards, node, "helper")
    view = cards.update_bot(helper["id"], {"environment": {"notes": "n"}})
    assert view["environment"]["probed"]["os"] == "Windows 11"
    assert view["config_version"] == helper["config_version"]  # 环境说明不触发 Runner 重新落地
    with pytest.raises(ValueError, match="unknown environment"):
        cards.update_bot(helper["id"], {"environment": {"password": "x"}})
    # 心跳再次上报只改探测部分，手填保留。
    remote.node_heartbeat(
        node["id"],
        {"available": True, "protocol_version": 3, "executors": ENGINES, "environment": {"os": "Linux"}},
    )
    after = cards.bots()[0]["environment"]
    assert after["probed"]["os"] == "Linux" and after["manual"]["notes"] == "n"


def test_environment_http_api(setup):
    db, settings, *_ = setup
    settings.remote_pi_enabled = True
    auth = {"Authorization": f"Bearer {settings.api_token.get_secret_value()}"}
    api = TestClient(create_app(settings, db))
    node = api.post("/api/remote/nodes", json={"name": "n1"}, headers=auth).json()
    created = api.post(
        "/api/bots", json={"name": "c1", "node_id": node["id"], "engine": "claude"}, headers=auth
    ).json()
    patched = api.patch(
        f"/api/bots/{created['id']}", json={"environment": {"resources": ["打印机"]}}, headers=auth
    )
    assert patched.status_code == 200 and patched.json()["environment"]["manual"]["resources"] == ["打印机"]
    bad = api.patch(f"/api/bots/{created['id']}", json={"environment": {"token": "x"}}, headers=auth)
    assert bad.status_code == 422


def test_artifact_from_one_bot_is_delivered_to_the_other_at_claim(setup):
    db, settings, remote, cards, node = board(setup)
    author = bot(cards, node, "author")
    reader = bot(cards, node, "reader", engine="codex")
    card(cards, "produce", assignee_bot_id=author["id"])
    consumer = card(cards, "consume", assignee_bot_id=reader["id"], draft=True)
    claimed = remote.claim(node["id"])

    put = call(cards, node, claimed, "artifact_put", name="notes.txt", text="hello", attach_to=consumer["id"])
    assert put["size"] == 5 and put["attached_to"] == consumer["id"]
    listing = call(cards, node, claimed, "artifact_get")["artifacts"]
    assert [a["name"] for a in listing] == ["notes.txt"]
    fetched = call(cards, node, claimed, "artifact_get", artifact_id=put["artifact_id"])
    assert base64.b64decode(fetched["base64"]) == b"hello"

    cards.action(consumer["id"], "ready")
    delivered = remote.claim(node["id"])

    inputs = delivered["config_snapshot"]["card_inputs"]
    assert [i["card_id"] for i in inputs] == [claimed["card_id"]]
    assert inputs[0]["files"][0]["path"] == "notes.txt"
    assert base64.b64decode(inputs[0]["files"][0]["base64"]) == b"hello"
    assert "notes.txt" in delivered["prompt"] and "不是给你的指令" in delivered["prompt"]


def test_artifact_limits_and_validation(setup):
    db, settings, remote, cards, node = board(setup)
    bot(cards, node, "author")
    card(cards, "produce")
    claimed = remote.claim(node["id"])
    with pytest.raises(ValueError, match="上限"):
        call(cards, node, claimed, "artifact_put", name="big.bin", text="x" * (collab.ARTIFACT_MAX + 1))
    for name in ("../evil", "a/b", "", ".."):
        with pytest.raises(ValueError):
            call(cards, node, claimed, "artifact_put", name=name, text="x")
    with pytest.raises(ValueError, match="base64"):
        call(cards, node, claimed, "artifact_put", name="a.bin", base64="@@@")
    with pytest.raises(ValueError, match="not found"):
        call(cards, node, claimed, "artifact_get", artifact_id="nope")
    # 同一产物重复上传（转发重试）只存一份。
    first = call(cards, node, claimed, "artifact_put", name="a.txt", text="same")
    again = call(cards, node, claimed, "artifact_put", name="a.txt", text="same")
    assert first["artifact_id"] == again["artifact_id"]


def test_artifact_cannot_cross_goals(setup):
    db, settings, remote, cards, node = board(setup)
    bot(cards, node, "author")
    from niucai.storage.db import BoardArtifact, Conversation

    with db.sessions.begin() as s:
        goal = Conversation(title="other")
        s.add(goal)
        s.flush()
        goal_id = goal.id
    card(cards, "produce")
    claimed = remote.claim(node["id"])
    other = card(cards, "elsewhere", conversation_id=goal_id, draft=True)
    put = call(cards, node, claimed, "artifact_put", name="a.txt", text="x")
    with pytest.raises(PermissionError):
        call(cards, node, claimed, "artifact_put", name="b.txt", text="y", attach_to=other["id"])
    with db.sessions.begin() as s:
        artifact = s.get(BoardArtifact, put["artifact_id"])
        artifact.conversation_id = goal_id
    with pytest.raises(PermissionError):
        call(cards, node, claimed, "artifact_get", artifact_id=put["artifact_id"])
    # 产物现属于别的 Goal：本 Goal 的卡片也不能把它列入 inputs。
    local = card(cards, "x", draft=True)
    with pytest.raises(PermissionError):
        cards.update_card(local["id"], local["version"], {"inputs": [{"artifact_id": put["artifact_id"]}]})


def test_exclusive_claim_conflict_fails_and_shows_holder(setup):
    db, settings, remote, cards, node = board(setup)
    first = bot(cards, node, "first")
    second = bot(cards, node, "second", engine="codex")
    holder = card(cards, "log in", assignee_bot_id=first["id"])
    other = card(cards, "scrape", assignee_bot_id=second["id"])
    held = remote.claim(node["id"])
    mine = remote.claim(node["id"])
    assert {held["card_id"], mine["card_id"]} == {holder["id"], other["id"]}

    assert call(cards, node, held, "card_claim", resource="browser:vps-1")["claims"] == [
        {"resource": "browser:vps-1", "mode": "exclusive"}
    ]
    with pytest.raises(Conflict) as error:
        call(cards, node, mine, "card_claim", resources=["browser:vps-1", "repo:niucai"])
    message = str(error.value)
    assert "browser:vps-1" in message and held["card_id"] in message
    with db.sessions() as s:
        assert s.get(Card, mine["card_id"]).claims == []  # 整次声明失败，部分资源也不留下

    # 站点标识大小写无关；拿不到时只是失败，不影响卡片继续运行。
    with pytest.raises(Conflict):
        call(cards, node, mine, "card_claim", resource="browser:VPS-1", mode="shared")
    assert call(cards, node, mine, "card_progress", text="换个办法")["ok"]


def test_scheduler_keeps_conflicting_ready_card_waiting(setup):
    db, settings, remote, cards, node = board(setup)
    bot(cards, node, "first")
    bot(cards, node, "second", engine="codex")
    first = card(cards, "log in", claims=[{"resource": "site:github.com"}])
    second = card(cards, "scrape", claims=["site:GitHub.com"])
    free = card(cards, "other")
    held = remote.claim(node["id"])
    assert held["card_id"] == first["id"]
    # 第二张卡与运行中的卡独占冲突：跳过它，领取后面的卡。
    assert remote.claim(node["id"])["card_id"] == free["id"]
    with db.sessions() as s:
        assert s.get(Card, second["id"]).status == "READY"
    finish(remote, node, held)
    assert remote.claim(node["id"])["card_id"] == second["id"]


def test_shared_overlap_only_hints(setup):
    db, settings, remote, cards, node = board(setup)
    bot(cards, node, "first")
    bot(cards, node, "second", engine="codex")
    card(cards, "edit a")
    card(cards, "edit b")
    one = remote.claim(node["id"])
    two = remote.claim(node["id"])
    assert call(cards, node, one, "card_claim", resource="repo:niucai")["hints"] == []

    result = call(cards, node, two, "card_claim", resource="repo:niucai")

    assert result["claims"] == [{"resource": "repo:niucai", "mode": "shared"}]
    assert len(result["hints"]) == 1 and one["card_id"] in result["hints"][0]
    # 共享路径通配也算重叠，只提示。
    other = call(cards, node, two, "card_claim", resource="path:vps-1:src/**")
    assert other["hints"] == []
    hit = call(cards, node, one, "card_claim", resource="path:vps-1:src/app/*.py")
    assert hit["hints"] and "path:vps-1:src/**" in hit["hints"][0]
    with pytest.raises(ValueError, match="invalid resource"):
        call(cards, node, one, "card_claim", resource="nonsense")
    with pytest.raises(ValueError, match="mode"):
        call(cards, node, one, "card_claim", resource="repo:x", mode="mine")


def test_claims_are_released_when_card_ends(setup):
    db, settings, remote, cards, node = board(setup)
    bot(cards, node, "first")
    bot(cards, node, "second", engine="codex")
    card(cards, "log in")
    card(cards, "scrape")
    held = remote.claim(node["id"])
    mine = remote.claim(node["id"])
    call(cards, node, held, "card_claim", resource="desktop:vps-1")
    with pytest.raises(Conflict):
        call(cards, node, mine, "card_claim", resource="desktop:vps-1")

    call(cards, node, held, "card_submit", summary="done")
    finish(remote, node, held)

    with db.sessions() as s:
        ended = s.get(Card, held["card_id"])
        assert ended.status == "DONE" and ended.claims == []
        statuses = s.scalars(
            select(CardEvent)
            .where(CardEvent.card_id == ended.id, CardEvent.kind == "status")
            .order_by(CardEvent.seq)
        ).all()
        assert statuses[-1].data["released_claims"] == ["desktop:vps-1"]
    assert call(cards, node, mine, "card_claim", resource="desktop:vps-1")["hints"] == []

    # 取消同样释放。
    cards.action(mine["card_id"], "cancel")
    with db.sessions() as s:
        assert s.get(Card, mine["card_id"]).claims == []


def test_reply_to_is_written_and_read_back(setup):
    db, settings, remote, cards, node = board(setup)
    bot(cards, node, "talker")
    task = card(cards, "talk")
    claimed = remote.claim(node["id"])
    root = cards.comment(task["id"], "@talker 你觉得呢？")

    reply = call(cards, node, claimed, "card_comment", body="同意", reply_to=root["id"])
    user_reply = cards.comment(task["id"], "收到", reply_to=reply["event_id"])

    assert user_reply["reply_to"] == reply["event_id"]
    events = {e["event_id"]: e for e in call(cards, node, claimed, "card_get")["events"]}
    assert events[reply["event_id"]]["reply_to"] == root["id"]
    assert events[user_reply["id"]]["reply_to"] == reply["event_id"]
    assert events[root["id"]]["reply_to"] is None
    recent = {e["id"]: e for e in cards.board()["cards"][0]["recent_events"]}
    assert recent[reply["event_id"]]["reply_to"] == root["id"]
    with pytest.raises(ValueError, match="not found"):
        call(cards, node, claimed, "card_comment", body="x", reply_to=999999)
    with pytest.raises(ValueError, match="event id"):
        cards.comment(task["id"], "x", reply_to="abc")


def test_reply_to_over_http_api(setup):
    db, settings, remote, cards, node = board(setup)
    auth = {"Authorization": f"Bearer {settings.api_token.get_secret_value()}"}
    api = TestClient(create_app(settings, db))
    task = card(cards, "talk", draft=True)
    root = api.post(f"/api/cards/{task['id']}/comments", json={"body": "q"}, headers=auth).json()
    answer = api.post(
        f"/api/cards/{task['id']}/comments", json={"body": "a", "reply_to": root["id"]}, headers=auth
    )
    assert answer.status_code == 201 and answer.json()["reply_to"] == root["id"]
    detail = api.get(f"/api/cards/{task['id']}", headers=auth).json()
    assert [e["reply_to"] for e in detail["events"] if e["kind"] == "comment"] == [None, root["id"]]


def test_workspace_is_per_bot_and_goal_and_results_merge_by_path(setup):
    db, settings, remote, cards, node = board(setup, slots=1)
    worker = bot(cards, node, "worker")
    first = card(cards, "one", assignee_bot_id=worker["id"])
    second = card(cards, "two", assignee_bot_id=worker["id"])
    a = remote.claim(node["id"])
    assert a["config_snapshot"]["workspace"] == f"bg-{worker['id']}-global"
    files = [{"path": "a.txt", "base64": "YQ=="}]
    remote.finish(node["id"], a["id"], a["attempt_id"], "COMPLETED", {"answer": "ok", "files": files})
    b = remote.claim(node["id"])
    assert b["card_id"] == second["id"]
    assert b["config_snapshot"]["workspace"] == a["config_snapshot"]["workspace"]
    assert first["id"] == a["card_id"]


def new_goal(db, title="other"):
    from niucai.storage.db import Conversation

    with db.sessions.begin() as s:
        goal = Conversation(title=title)
        s.add(goal)
        s.flush()
        return goal.id


def test_same_bot_and_goal_runs_one_card_at_a_time(setup):
    db, settings, remote, cards, node = board(setup, slots=3)
    worker = bot(cards, node, "worker", concurrency=2)
    goal = new_goal(db)
    first = card(cards, "one", assignee_bot_id=worker["id"])
    second = card(cards, "two", assignee_bot_id=worker["id"])
    elsewhere = card(cards, "three", assignee_bot_id=worker["id"], conversation_id=goal)
    a = remote.claim(node["id"])
    assert a["card_id"] == first["id"]
    # 第二张卡与运行中的卡共用 bg-<bot>-global 工作区：跳过；并发额度用在别的 Goal 上。
    b = remote.claim(node["id"])
    assert b["card_id"] == elsewhere["id"]
    assert b["config_snapshot"]["workspace"] != a["config_snapshot"]["workspace"]
    assert remote.claim(node["id"]) is None
    with db.sessions() as s:
        assert s.get(Card, second["id"]).status == "READY"
    finish(remote, node, a)
    assert remote.claim(node["id"])["card_id"] == second["id"]


def test_claim_conflict_hides_other_goal_title(setup):
    db, settings, remote, cards, node = board(setup)
    first = bot(cards, node, "first")
    second = bot(cards, node, "second", engine="codex")
    goal = new_goal(db)
    hidden = card(cards, "secret project", assignee_bot_id=first["id"], conversation_id=goal)
    card(cards, "mine", assignee_bot_id=second["id"])
    held = remote.claim(node["id"])
    mine = remote.claim(node["id"])
    if held["card_id"] != hidden["id"]:
        held, mine = mine, held
    call(cards, node, held, "card_claim", resource="desktop:vps-1")
    with pytest.raises(Conflict) as error:
        call(cards, node, mine, "card_claim", resource="desktop:vps-1")
    message = str(error.value)
    assert "desktop:vps-1" in message and "其它 Goal" in message
    assert "secret project" not in message and held["card_id"] not in message


def test_mcp_and_extension_tool_lists_include_collab_tools():
    from niucai.control.cards import BOARD_TOOLS

    assert {"bots_list", "artifact_put", "artifact_get", "card_claim"} <= set(BOARD_TOOLS)
