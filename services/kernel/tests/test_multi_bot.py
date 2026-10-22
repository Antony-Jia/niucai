from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from niucai.actions.adapters import FakeAdapter
from niucai.actions.gateway import ActionGateway
from niucai.api.app import create_app
from niucai.context.compiler import ContextCompiler
from niucai.control.collaboration import CollaborationManager, gate
from niucai.control.conversations import ConversationManager
from niucai.control.tasks import Conflict
from niucai.domain.schemas import TaskCreate
from niucai.storage.db import Agent, Handoff, Memory, Task, Workflow, WorkItem, now
from niucai.worker import Worker


def prepare(setup):
    db, settings, tasks, computer = setup
    settings.multi_bot_enabled = True
    with db.sessions.begin() as session:
        for ident in ["writer", "reviewer"]:
            session.add(
                Agent(
                    id=ident,
                    name=ident,
                    definition={
                        "instructions": ident + " playbook",
                        "tools": ["files.read", "bot_*", "board_*"],
                    },
                )
            )
    task = tasks.create(TaskCreate(title="aggregate", goal="aggregate"), unified=True)
    task = tasks.claim()
    return db, settings, tasks, task, CollaborationManager(db, settings)


def consume(db, task):
    inbox = ConversationManager(db)
    for message in inbox.pending(task.id):
        inbox.acknowledge(task.id, task.run_token, message.id, message.id)


def complete(db, tasks, task, content):
    consume(db, task)
    return tasks.update(task.id, task.run_token, status="COMPLETED", checkpoint={"result": content})


def test_all_wait_release_and_wake(setup):
    db, settings, tasks, root, manager = prepare(setup)
    consume(db, root)
    b = manager.send(root.id, root.run_token, "b", "writer", "write")
    c = manager.send(root.id, root.run_token, "c", "reviewer", "review")
    wait = manager.wait(root.id, root.run_token, "wait", [b["id"], c["id"]])
    assert wait["status"] == "WAITING_BOT"
    tasks.update(root.id, root.run_token, status="WAITING_BOT", checkpoint=wait["wait"])
    child = tasks.claim()
    complete(db, tasks, child, "B result")
    with db.sessions() as session:
        assert session.get(Task, root.id).status == "WAITING_BOT"
    child = tasks.claim()
    complete(db, tasks, child, "C result")
    resumed = tasks.claim()
    assert resumed.id == root.id
    results = manager.wait(root.id, resumed.run_token, "wait", [b["id"], c["id"]])
    assert len(results["results"]) == 2
    complete(db, tasks, resumed, "aggregated")
    with db.sessions() as session:
        assert session.scalar(select(Workflow)).status == "COMPLETED"


def test_reply_before_wait_and_idempotency(setup):
    db, settings, tasks, root, manager = prepare(setup)
    b = manager.send(root.id, root.run_token, "b", "writer", "write")
    assert manager.send(root.id, root.run_token, "b", "writer", "write")["id"] == b["id"]
    with pytest.raises(Conflict):
        manager.send(root.id, root.run_token, "b", "writer", "changed")
    child = tasks.claim()
    complete(db, tasks, child, "early")
    assert manager.wait(root.id, root.run_token, "w", [b["id"]])["results"][0]["reply"]["content"] == "early"


def test_wait_reply_race(setup):
    db, settings, tasks, root, manager = prepare(setup)
    consume(db, root)
    b = manager.send(root.id, root.run_token, "b", "writer", "write")
    wait = manager.wait(root.id, root.run_token, "w", [b["id"]])
    child = tasks.claim()
    complete(db, tasks, child, "race")
    waiting = tasks.update(root.id, root.run_token, status="WAITING_BOT", checkpoint=wait["wait"])
    assert waiting.status == "PENDING"


def test_receiver_identity_and_fencing(setup):
    db, settings, tasks, root, manager = prepare(setup)
    b = manager.send(root.id, root.run_token, "b", "writer", "write")
    with pytest.raises(PermissionError):
        manager.reply(root.id, root.run_token, b["id"], "forged")
    child = tasks.claim()
    with pytest.raises(Conflict):
        manager.reply(child.id, "old token", b["id"], "stale")


def test_timeout_hard_limit_and_stop(setup):
    db, settings, tasks, root, manager = prepare(setup)
    b = manager.send(root.id, root.run_token, "b", "writer", "write")
    with db.sessions.begin() as session:
        session.get(Handoff, b["id"]).deadline = now() - timedelta(seconds=1)
    manager.tick()
    assert manager.wait(root.id, root.run_token, "w", [b["id"]])["results"][0]["status"] == "TIMED_OUT"
    with db.sessions.begin() as session:
        workflow = session.scalar(select(Workflow))
        workflow.limits = {**workflow.limits, "handoffs": 1}
    assert manager.send(root.id, root.run_token, "c", "reviewer", "review")["status"] == "FAILED"
    with db.sessions() as session:
        assert session.scalar(select(Workflow)).status == "FAILED"
    with db.sessions.begin() as session:
        gate(session).stopped = 1
    assert tasks.claim() is None


def test_identity_permissions_and_memory_scope(setup):
    db, settings, tasks, root, manager = prepare(setup)
    manager.send(root.id, root.run_token, "b", "writer", "write")
    child = tasks.claim()
    with db.sessions.begin() as session:
        for owner in [None, "writer", "reviewer"]:
            session.add(Memory(kind="semantic", content="write knowledge", agent_id=owner))
    package = ContextCompiler(db, settings).compile(child.id)
    assert package["identity"]["instructions"] == "writer playbook"
    assert package["available_tools"] == ["files.read"]
    assert len(package["memories"]) == 2
    gateway = ActionGateway(db, settings, FakeAdapter())
    with pytest.raises(PermissionError):
        gateway.propose(child.id, child.run_token, {"type": "files.write", "path": "x", "content": "x"}, "x")


@pytest.mark.parametrize(
    "reason", ["agent requested human", "remote Pi approval required", "bot dependencies"]
)
def test_approval_does_not_wake_other_waits(setup, reason):
    db, settings, tasks, computer = setup
    root = tasks.create(TaskCreate(title="test", goal="test", computer_id=computer))
    root = tasks.claim()
    gateway = ActionGateway(db, settings, FakeAdapter())
    action = gateway.propose(
        root.id, root.run_token, {"type": "files.write", "path": "a", "content": "a"}, "a"
    )
    from niucai.storage.db import Approval

    with db.sessions() as session:
        approval = session.scalar(select(Approval).where(Approval.action_id == action.id))
    tasks.update(root.id, root.run_token, status="WAITING_HUMAN", checkpoint={"reason": reason})
    gateway.decide(approval.id, True)
    with db.sessions() as session:
        assert session.get(Task, root.id).status == "WAITING_HUMAN"


def test_sqlite_slots_rejected(tmp_path):
    from niucai.config import Settings
    from niucai.storage.db import Database

    settings = Settings(database_url=f"sqlite:///{tmp_path}/slots.db", worker_slots=2)
    db = Database(settings.database_url)
    with pytest.raises(ValueError, match="SQLite"):
        Worker(db, settings, None, FakeAdapter())


def test_agent_crud_api(setup):
    db, settings, tasks, computer = setup
    settings.multi_bot_enabled = True
    with TestClient(create_app(settings, db)) as client:
        client.headers["Authorization"] = "Bearer " + "x" * 40
        assert client.post("/api/agents", json={"id": "writer", "name": "Writer"}).status_code == 201
        assert client.get("/api/agents/writer").json()["definition"]["allowed_nodes"] == []
        assert client.delete("/api/agents/main").status_code == 409
        assert client.delete("/api/agents/writer").json()["definition"]["archived"] is True


def test_any_retains_remaining_results_and_pause_does_not_wake(setup):
    db, settings, tasks, root, manager = prepare(setup)
    consume(db, root)
    b = manager.send(root.id, root.run_token, "b", "writer", "write")
    c = manager.send(root.id, root.run_token, "c", "reviewer", "review")
    condition = manager.wait(root.id, root.run_token, "w", [b["id"], c["id"]], "any")
    tasks.update(root.id, root.run_token, status="WAITING_BOT", checkpoint=condition["wait"])
    tasks.transition(root.id, "pause")
    complete(db, tasks, tasks.claim(), "B")
    with db.sessions() as session:
        assert session.get(Task, root.id).status == "PAUSED"
    assert tasks.transition(root.id, "resume").status == "PENDING"
    resumed = tasks.claim()
    assert len(manager.wait(root.id, resumed.run_token, "w", [b["id"], c["id"]], "any")["results"]) == 1
    complete(db, tasks, resumed, "first")
    complete(db, tasks, tasks.claim(), "second")
    with db.sessions() as session:
        assert session.get(Handoff, c["id"]).reply["content"] == "second"


def test_stage_transfer_and_revision_limits(setup):
    db, settings, tasks, root, manager = prepare(setup)
    consume(db, root)
    with db.sessions.begin() as session:
        gate(session)
        workflow, _ = manager.root(session, session.get(Task, root.id))
        workflow.routes = [["main", "writer"], ["writer", "reviewer"], ["reviewer", "writer"]]
        workflow.limits = {**workflow.limits, "revisions": 1, "final_agent_ids": ["reviewer"]}
    with pytest.raises(PermissionError):
        manager.send(root.id, root.run_token, "bad", "reviewer", "bad", "TRANSFER", False, "stage", "review")
    manager.send(
        root.id, root.run_token, "transfer", "writer", "write", "TRANSFER", False, "research", "draft"
    )
    with db.sessions() as session:
        assert session.get(Task, root.id).status == "COMPLETED"
    writer = tasks.claim()
    consume(db, writer)
    manager.send(
        writer.id, writer.run_token, "review", "reviewer", "review", "TRANSFER", False, "draft", "verdict"
    )
    with db.sessions() as session:
        assert session.get(Task, writer.id).status == "COMPLETED"
    reviewer = tasks.claim()
    complete(db, tasks, reviewer, "approved")
    with db.sessions() as session:
        assert session.scalar(select(Workflow)).status == "COMPLETED"
        assert session.get(Task, root.id).status == "COMPLETED"


def test_routine_dedup_overlap_and_gate(setup, monkeypatch):
    db, settings, tasks, root, manager = prepare(setup)
    from niucai.control.routines import fire_due, next_time
    from niucai.storage.db import Routine, RoutineRun

    settings.routines_enabled = True
    with db.sessions.begin() as session:
        routine = Routine(
            agent_id="writer",
            schedule="* * * * *",
            timezone="Asia/Shanghai",
            prompt="write",
            next_run_at=now() - timedelta(minutes=5),
        )
        session.add(routine)
        session.flush()
        ident = routine.id
    fire_due(db, settings)
    fire_due(db, settings)
    with db.sessions.begin() as session:
        assert len(list(session.scalars(select(RoutineRun)))) == 1
        session.get(Routine, ident).next_run_at = now() - timedelta(minutes=1)
    future = now() + timedelta(minutes=2)
    monkeypatch.setattr("niucai.control.routines.now", lambda: future)
    fire_due(db, settings)
    with db.sessions() as session:
        assert sorted(r.status for r in session.scalars(select(RoutineRun))) == ["ADMITTED", "SKIPPED"]
    assert next_time("0 9 * * *", "Asia/Shanghai").hour == 1


def test_routine_dst_gap_and_fold():
    from niucai.control.routines import next_time

    assert next_time("30 2 * * *", "America/New_York", datetime(2026, 3, 8, 6)) == datetime(2026, 3, 8, 7)
    assert next_time("30 1 * * *", "America/New_York", datetime(2026, 11, 1, 5, 31)) == datetime(
        2026, 11, 1, 6, 30
    )


def test_stage_revision_cap_fails_whole_workflow(setup):
    db, settings, tasks, root, manager = prepare(setup)
    settings.handoff_max_hops = 10
    consume(db, root)
    with db.sessions.begin() as session:
        gate(session)
        workflow, _ = manager.root(session, session.get(Task, root.id))
        workflow.routes = [["main", "writer"], ["writer", "reviewer"], ["reviewer", "writer"]]
        workflow.limits = {**workflow.limits, "revisions": 1, "final_agent_ids": ["reviewer"]}
    owner = root
    for index, to in enumerate(["writer", "reviewer", "writer"]):
        consume(db, owner)
        manager.send(
            owner.id,
            owner.run_token,
            str(index),
            to,
            "next",
            "TRANSFER",
            False,
            "stage result",
            "next output",
        )
        owner = tasks.claim()
    consume(db, owner)
    result = manager.send(
        owner.id, owner.run_token, "too-many", "reviewer", "next", "TRANSFER", False, "revised", "approval"
    )
    assert result["status"] == "FAILED"
    with db.sessions() as session:
        assert session.scalar(select(Workflow)).status == "FAILED"
        assert not session.scalar(
            select(Task.id).where(Task.status.in_(["PENDING", "RUNNING", "WAITING_BOT"]))
        )


def test_postgres_multiple_claimers_and_computer_mutex(setup):
    from concurrent.futures import ThreadPoolExecutor

    db, settings, tasks, computer = setup
    if db.engine.dialect.name != "postgresql":
        pytest.skip("requires dedicated NIUCAI_TEST_DATABASE_URL PostgreSQL")
    settings.multi_bot_enabled = True
    for i in range(2):
        tasks.create(TaskCreate(title=f"computer-{i}", goal="work", computer_id=computer), unified=True)
    for i in range(2):
        tasks.create(TaskCreate(title=f"reason-{i}", goal="think"), unified=True)
    with ThreadPoolExecutor(max_workers=4) as pool:
        owned = [row for row in pool.map(lambda _: tasks.claim(), range(4)) if row]
    assert len(owned) == 3
    assert len({row.id for row in owned}) == 3
    assert sum(row.computer_id == computer for row in owned) == 1


def test_postgres_reply_wait_race_and_idempotent_send(setup):
    from concurrent.futures import ThreadPoolExecutor

    if setup[0].engine.dialect.name != "postgresql":
        pytest.skip("requires dedicated NIUCAI_TEST_DATABASE_URL PostgreSQL")
    db, settings, tasks, root, manager = prepare(setup)
    consume(db, root)
    with ThreadPoolExecutor(max_workers=2) as pool:
        sent = list(
            pool.map(lambda _: manager.send(root.id, root.run_token, "same", "writer", "work"), range(2))
        )
    assert sent[0]["id"] == sent[1]["id"]
    child = tasks.claim()
    consume(db, child)
    with ThreadPoolExecutor(max_workers=2) as pool:
        wait = pool.submit(manager.wait, root.id, root.run_token, "wait", [sent[0]["id"]])
        reply = pool.submit(manager.reply, child.id, child.run_token, sent[0]["id"], "result")
        waiting = wait.result(timeout=15)
        reply.result(timeout=15)
    if waiting["status"] == "WAITING_BOT":
        tasks.update(root.id, root.run_token, status="WAITING_BOT", checkpoint=waiting["wait"])
    with db.sessions() as session:
        assert session.get(Task, root.id).status in {"RUNNING", "PENDING"}
        assert session.scalar(select(Handoff)).status == "REPLIED"


def test_group_routes_broadcast_and_execution_isolation(setup):
    db, settings, tasks, root, manager = prepare(setup)
    from niucai.control.groups import send_group
    from niucai.storage.db import ConversationParticipant, Message

    with db.sessions.begin() as session:
        display = ConversationManager.create_in(session)
        for ident in ["main", "writer", "reviewer"]:
            session.add(
                ConversationParticipant(
                    conversation_id=display.id,
                    agent_id=ident,
                    role="coordinator" if ident == "main" else "member",
                )
            )
        display_id = display.id
    display, message, first = send_group(db, settings, display_id, "@everyone discuss", "round")
    assert send_group(db, settings, display_id, "@everyone discuss", "round")[2].id == first.id
    with db.sessions() as session:
        workflow = session.scalar(select(Workflow))
        items = list(session.scalars(select(WorkItem).where(WorkItem.workflow_id == workflow.id)))
        assert len(items) == 3
        assert len({i.conversation_id for i in items}) == 3
    for expected in ["main", "reviewer", "writer"]:
        claimed = tasks.claim()
        assert claimed.agent_id == expected
        complete(db, tasks, claimed, "public result " + expected)
    with db.sessions() as session:
        assert len(list(session.scalars(select(Message).where(Message.conversation_id == display_id)))) == 4
    with pytest.raises(Conflict):
        send_group(db, settings, display_id, "@outsider nope", "other")


def test_board_version_scope_and_audit(setup):
    db, settings, tasks, root, manager = prepare(setup)
    from niucai.control.board import board_tool

    manager.send(root.id, root.run_token, "b", "writer", "write")
    items = board_tool(db, root.id, root.run_token, "board.list", {})
    item = items[0]
    board_tool(
        db,
        root.id,
        root.run_token,
        "board.update",
        {"work_item_id": item["id"], "expected_version": item["version"], "labels": ["draft"]},
    )
    with pytest.raises(Conflict):
        board_tool(
            db,
            root.id,
            root.run_token,
            "board.update",
            {"work_item_id": item["id"], "expected_version": item["version"], "labels": ["stale"]},
        )


def test_remote_artifact_transfer_and_waiting_parent(setup):
    import base64
    from hashlib import sha256

    from niucai.control.artifacts import reference, snapshot
    from niucai.control.remote import RemoteManager
    from niucai.storage.db import Artifact

    db, settings, tasks, root, manager = prepare(setup)
    settings.remote_pi_enabled = True
    handoff = manager.send(root.id, root.run_token, "b", "writer", "write")
    with db.sessions.begin() as session:
        artifact = snapshot(session, settings, root.id, b"source", "text/plain")
        ref = reference(artifact)
    with db.sessions.begin() as session:
        workflow = session.scalar(select(Workflow))
        with pytest.raises(Conflict, match="version"):
            manager.artifact_refs(session, workflow.id, [{**ref, "version": 2}])
    remote = RemoteManager(db, settings)
    node = remote.create_node("node")
    other = remote.create_node("other")
    remote.node_heartbeat(node["id"], {"executor": "pi", "available": True})
    job = remote.submit("review source", "artifact-job", [node["id"]], root.id, root.run_token, [ref])
    remote.control(job["id"], "approve")
    claimed = remote.claim(node["id"])
    content, media = remote.artifact_content(node["id"], job["id"], claimed["attempt_id"], ref["id"])
    assert content == b"source" and media == "text/plain"
    with pytest.raises(Conflict):
        remote.artifact_content(other["id"], job["id"], claimed["attempt_id"], ref["id"])
    wait = manager.wait(root.id, root.run_token, "w", [handoff["id"]])
    tasks.update(root.id, root.run_token, status="WAITING_BOT", checkpoint=wait["wait"])
    assert remote.heartbeat(node["id"], job["id"], claimed["attempt_id"])["stop"] is False
    result = remote.finish(
        node["id"],
        job["id"],
        claimed["attempt_id"],
        "COMPLETED",
        {
            "files": [
                {
                    "path": "../../untrusted",
                    "base64": base64.b64encode(b"output").decode(),
                    "content_hash": sha256(b"output").hexdigest(),
                    "media_type": "text/plain",
                }
            ]
        },
    )
    output = result["result"]["artifacts"][0]
    with db.sessions() as session:
        record = session.get(Artifact, output["id"])
        assert (settings.workspace / record.path).read_bytes() == b"output"
        assert "untrusted" not in record.path


def test_memory_scope_shared_permission_and_safe_replay(setup):
    from niucai.control.memory import memory_tool

    db, settings, tasks, root, manager = prepare(setup)
    with db.sessions.begin() as session:
        writer = session.get(Agent, "writer")
        writer.definition = {**writer.definition, "tools": ["memory.*"]}
    manager.send(root.id, root.run_token, "b", "writer", "write")
    child = tasks.claim()
    with db.sessions.begin() as session:
        writer = session.get(Agent, "writer")
        private = Memory(agent_id="reviewer", kind="semantic", content="private")
        session.add(private)
        session.flush()
        foreign_id = private.id
    params = {"content": "own knowledge", "toolTaskId": "remember"}
    first = memory_tool(db, child.id, child.run_token, "memory.create", params)
    assert memory_tool(db, child.id, child.run_token, "memory.create", params)["id"] == first["id"]
    with pytest.raises(PermissionError):
        memory_tool(db, child.id, child.run_token, "memory.update", {"id": foreign_id, "content": "forged"})
    with db.sessions.begin() as session:
        writer = session.get(Agent, "writer")
        writer.definition = {**writer.definition, "tools": ["memory.create"]}
    with pytest.raises(PermissionError):
        memory_tool(db, child.id, child.run_token, "memory.create", {"content": "shared", "shared": True})


def test_explicit_reply_finishes_task_and_fences_side_effects(setup):
    db, settings, tasks, root, manager = prepare(setup)
    handoff = manager.send(root.id, root.run_token, "b", "writer", "write")
    child = tasks.claim()
    result = manager.reply(child.id, child.run_token, handoff["id"], "finished")
    assert result["complete"] == "finished"
    with db.sessions() as session:
        current = session.get(Task, child.id)
        assert current.status == "COMPLETED" and current.run_token is None
    with pytest.raises(Conflict):
        ActionGateway(db, settings, FakeAdapter()).propose(
            child.id, child.run_token, {"type": "files.read", "path": "x"}, "late"
        )


def test_chat_continues_after_delegation_and_reports_execution_conversation(setup):
    db, settings, tasks, root, manager = prepare(setup)
    consume(db, root)
    manager.send(root.id, root.run_token, "b", "writer", "write")
    complete(db, tasks, root, "done")
    complete(db, tasks, tasks.claim(), "B")
    with db.sessions() as session:
        chat = session.get(Task, root.id).conversation_id
        assert session.scalar(select(WorkItem).where(WorkItem.task_id == root.id)).conversation_id is None
    ConversationManager(db).send(chat, "follow up", None)
    follow = tasks.claim()
    assert follow.conversation_id == chat and follow.id != root.id
    consume(db, follow)
    assert manager.send(follow.id, follow.run_token, "b2", "writer", "again")["status"] == "ACCEPTED"
    with TestClient(create_app(settings, db)) as client:
        client.headers["Authorization"] = "Bearer " + "x" * 40
        roots = [i for i in client.get("/api/work-items").json() if i["task_id"] in {root.id, follow.id}]
        assert [i["conversation_id"] for i in roots] == [chat, chat]


def test_skipped_claim_keeps_work_item_queued(setup):
    db, settings, tasks, root, manager = prepare(setup)
    computer = setup[3]
    from niucai.storage.db import Computer

    blocker = tasks.create(TaskCreate(title="blocker", goal="hold", computer_id=computer), unified=True)
    handoff = manager.send(root.id, root.run_token, "b", "writer", "write")
    with db.sessions.begin() as session:
        session.get(Task, blocker.id).status = "PAUSED"
        session.get(Computer, computer).active_task_id = blocker.id
        session.get(Task, handoff["to_task_id"]).computer_id = computer
    assert tasks.claim() is None
    with db.sessions() as session:
        assert session.get(WorkItem, handoff["to_work_item_id"]).status == "QUEUED"


def test_final_stage_ignores_late_delegate_branch(setup):
    db, settings, tasks, root, manager = prepare(setup)
    consume(db, root)
    with db.sessions.begin() as session:
        gate(session)
        workflow, _ = manager.root(session, session.get(Task, root.id))
        workflow.routes = [["main", "reviewer"]]
        workflow.limits = {**workflow.limits, "final_agent_ids": ["reviewer"]}
    manager.send(root.id, root.run_token, "t", "reviewer", "review", "TRANSFER", False, "draft", "verdict")
    reviewer = tasks.claim()
    consume(db, reviewer)
    manager.send(reviewer.id, reviewer.run_token, "helper", "writer", "check facts")
    complete(db, tasks, reviewer, "approved")
    complete(db, tasks, tasks.claim(), "late facts")
    with db.sessions() as session:
        assert session.scalar(select(Workflow)).status == "COMPLETED"


def test_group_mention_ignores_email_address(setup):
    db, settings, tasks, root, manager = prepare(setup)
    from niucai.control.groups import send_group
    from niucai.storage.db import ConversationParticipant

    with db.sessions.begin() as session:
        display = ConversationManager.create_in(session)
        for ident in ["main", "writer"]:
            session.add(
                ConversationParticipant(
                    conversation_id=display.id,
                    agent_id=ident,
                    role="coordinator" if ident == "main" else "member",
                )
            )
        display_id = display.id
    first = send_group(db, settings, display_id, "mail ops@example.com please", "mail")[2]
    assert first.agent_id == "main"


def test_handoff_deadline_paused_while_receiver_waits_for_human(setup):
    db, settings, tasks, root, manager = prepare(setup)
    handoff = manager.send(root.id, root.run_token, "b", "writer", "write")
    child = tasks.claim()
    tasks.update(
        child.id, child.run_token, status="WAITING_HUMAN", checkpoint={"reason": "agent requested human"}
    )
    with db.sessions.begin() as session:
        session.get(Handoff, handoff["id"]).deadline = now() - timedelta(seconds=1)
    manager.tick()
    with db.sessions() as session:
        row = session.get(Handoff, handoff["id"])
        assert row.status == "ACCEPTED" and row.human_wait_started_at is not None


@pytest.mark.parametrize("restore", ["task", "workflow"])
def test_human_resume_compensates_deadline_before_tick(setup, monkeypatch, restore):
    import niucai.control.collaboration as collaboration

    db, settings, tasks, root, manager = prepare(setup)
    instant = now()
    monkeypatch.setattr(collaboration, "now", lambda: instant)
    handoff = manager.send(root.id, root.run_token, "b", "writer", "write")
    child = tasks.claim()
    tasks.update(child.id, child.run_token, status="WAITING_HUMAN")
    with db.sessions() as session:
        original = session.get(Handoff, handoff["id"]).deadline
    if restore == "workflow":
        with TestClient(create_app(settings, db)) as client:
            client.headers["Authorization"] = "Bearer " + "x" * 40
            assert client.post(f"/api/workflows/{handoff['workflow_id']}/pause").status_code == 200
            instant = original + timedelta(seconds=1)
            assert client.post(f"/api/workflows/{handoff['workflow_id']}/resume").status_code == 200
    else:
        instant = original + timedelta(seconds=1)
        tasks.transition(child.id, "resume")
    manager.tick()
    with db.sessions() as session:
        row = session.get(Handoff, handoff["id"])
        assert row.status == "ACCEPTED"
        assert row.deadline > instant
        assert row.human_wait_started_at is None
        assert session.get(Task, child.id).status == "PENDING"


def test_approval_resume_compensates_deadline_before_tick(setup, monkeypatch):
    import niucai.control.collaboration as collaboration
    from niucai.storage.db import Action, Approval

    db, settings, tasks, root, manager = prepare(setup)
    instant = now()
    monkeypatch.setattr(collaboration, "now", lambda: instant)
    handoff = manager.send(root.id, root.run_token, "b", "writer", "write")
    child = tasks.claim()
    with db.sessions.begin() as session:
        session.add(
            Action(
                id="approval-action",
                task_id=child.id,
                agent_id="writer",
                idempotency_key="approval-action",
                spec={},
                risk="HIGH",
                status="WAITING_APPROVAL",
            )
        )
        session.flush()
        session.add(Approval(id="approval", action_id="approval-action", status="PENDING"))
    tasks.update(
        child.id,
        child.run_token,
        status="WAITING_HUMAN",
        checkpoint={"reason": "WAITING_APPROVAL", "action_id": "approval-action"},
    )
    with db.sessions() as session:
        instant = session.get(Handoff, handoff["id"]).deadline + timedelta(seconds=1)
    ActionGateway(db, settings, FakeAdapter()).decide("approval", True)
    manager.tick()
    with db.sessions() as session:
        assert session.get(Handoff, handoff["id"]).status == "ACCEPTED"
        assert session.get(Task, child.id).status == "PENDING"


def test_expired_run_is_not_reported_as_exited(setup):
    from niucai.storage.db import TaskRun

    db, settings, tasks, root, manager = prepare(setup)
    with db.sessions.begin() as session:
        session.get(Task, root.id).lease_until = now() - timedelta(seconds=1)
    assert tasks.claim().id == root.id
    with db.sessions() as session:
        old = session.get(TaskRun, root.run_token)
        assert old.status == "EXPIRED"
        assert old.process_exited_at is None
    with db.sessions.begin() as session:
        gate(session).stopped = 1
        session.get(TaskRun, root.run_token).exit_tracking_started_at = None
        for run in session.scalars(select(TaskRun).where(TaskRun.id != root.run_token)):
            run.process_exited_at = now()
    with TestClient(create_app(settings, db)) as client:
        client.headers["Authorization"] = "Bearer " + "x" * 40
        response = client.get("/api/collaboration/status").json()
        assert response["resource_release"] == "unknown"
        assert response["legacy_untracked_runs"] == [root.run_token]
        assert response["unconfirmed_local_runs"] == []
