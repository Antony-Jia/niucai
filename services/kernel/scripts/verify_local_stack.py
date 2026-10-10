"""Opt-in acceptance against the isolated compose.local-test.yml stack.

Uses real API, Worker, Pi and configured model. Approves only the named test file.
The API token is read from an ignored local dotenv file and never logged.
"""

import argparse
import json
import time
from pathlib import Path
from uuid import uuid4

import httpx
from dotenv import dotenv_values, set_key


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    config = dotenv_values(args.config)
    base = "http://127.0.0.1:38080"
    report = {"environment": "isolated local Docker API + Pi + PostgreSQL + Computer"}
    client = httpx.Client(
        base_url=base, headers={"Authorization": f"Bearer {config['NIUCAI_API_TOKEN']}"}, timeout=30
    )

    def request(method, path, body=None, status=200):
        response = client.request(method, path, json=body)
        if response.status_code != status:
            raise RuntimeError(
                f"{method} {path}: expected {status}, got {response.status_code}: {response.text[:500]}"
            )
        return response.json()

    def task(ident):
        value = request("GET", f"/api/tasks/{ident}")
        value["actions"] = request("GET", f"/api/tasks/{ident}/actions")
        return value

    def wait(ident, predicate, timeout=240):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = task(ident)
            if predicate(value):
                return value
            if value["status"] in {"FAILED", "CANCELLED"}:
                raise RuntimeError(f"Round {ident} ended in {value['status']}: {value['checkpoint']}")
            time.sleep(0.25)
        raise RuntimeError(f"Round timed out: {task(ident)}")

    def finish(ident):
        return wait(ident, lambda value: value["status"] == "COMPLETED")

    def timeline(cid):
        items = []
        cursor = 0
        while True:
            page = request("GET", f"/api/conversations/{cid}/timeline?after={cursor}&limit=20")
            items.extend(page["items"])
            if not page["has_more"]:
                return {**page, "items": items}
            assert page["next_cursor"] > cursor
            cursor = page["next_cursor"]

    def model_started(cid, ident):
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if any(i["type"] == "model.requested" and i["task_id"] == ident for i in timeline(cid)["items"]):
                return
            time.sleep(0.1)
        raise RuntimeError("Model request did not start")

    def send(cid, content, client_id=None):
        return request(
            "POST",
            f"/api/conversations/{cid}/messages",
            {"content": content, "client_id": client_id or str(uuid4())},
            202,
        )

    request("GET", "/healthz")
    assert httpx.get(base + "/api/conversations").status_code == 401
    computers = request("GET", "/api/computers")
    computer = next((c for c in computers if c["name"] == "local-unified"), None)
    if not computer:
        computer = request("POST", "/api/computers", {"name": "local-unified", "kind": "linux"}, 201)
    set_key(args.config, "NIUCAI_COMPUTER_WEB_ID", computer["id"], quote_mode="always")
    report["computer_id"] = computer["id"]

    cid = request(
        "POST",
        "/api/conversations",
        {"title": "Local Docker unified acceptance", "computer_id": computer["id"]},
        201,
    )["id"]
    report["conversation_id"] = cid
    key = str(uuid4())
    first = send(cid, "Remember the verification word blue for later. Reply briefly without tools.", key)
    duplicate = send(cid, "Remember the verification word blue for later. Reply briefly without tools.", key)
    assert first["messages"][0]["id"] == duplicate["messages"][0]["id"]
    assert first["task"]["id"] == duplicate["task"]["id"]
    completed = finish(first["task"]["id"])
    assert not completed["actions"]
    report["discussion_idempotency"] = completed["id"]
    print("discussion and idempotent admission: passed", flush=True)

    second = send(
        cid,
        "Open http://web/local-test-proof.html in the real browser. "
        "Read its title and text using browser.snapshot. Do not write files. Reply briefly.",
    )
    tid = second["task"]["id"]
    model_started(cid, tid)
    addition = send(
        cid,
        "Additional requirement: include the remembered verification word blue "
        "and the exact marker STEER_OK in the final reply. Still read the browser page.",
    )
    assert addition["task"]["id"] == tid
    completed = finish(tid)
    result = completed["checkpoint"]["result"]
    assert "blue" in result.lower() and "STEER_OK" in result
    assert (
        completed["checkpoint"]["pi_conversation_id"]
        == task(first["task"]["id"])["checkpoint"]["pi_conversation_id"]
    )
    assert any(
        a["spec"]["type"] == "browser.snapshot" and a["status"] == "SUCCEEDED" for a in completed["actions"]
    )
    report["running_steer_shared_context"] = tid
    preview = client.get(f"/api/computers/{computer['id']}/preview")
    assert preview.status_code == 200
    assert preview.json()["image"].startswith("data:image/jpeg;base64,")
    print("real browser, running steer and shared context: passed", flush=True)

    third = send(cid, "Write blue to local-unified-proof.txt using files.write and do nothing else.")
    tid = third["task"]["id"]
    pending = wait(tid, lambda value: any(a["status"] == "WAITING_APPROVAL" for a in value["actions"]))
    approvals = [p for p in request("GET", "/api/approvals") if p["action"]["task_id"] == tid]
    assert len(approvals) == 1
    stale_id = approvals[0]["id"]
    send(
        cid,
        "Change the file content to blue APPROVAL_REVISED. "
        "The only permitted output path is still local-unified-proof.txt. Write once after approval.",
    )
    assert (
        client.post(
            f"/api/approvals/{stale_id}/approve", json={"note": "stale proposal must fail"}
        ).status_code
        == 409
    )
    pending = task(tid)
    assert all(a["status"] != "SUCCEEDED" for a in pending["actions"] if a["spec"]["type"] == "files.write")
    if "resume" in pending["progress"]["allowed_operations"]:
        request("POST", f"/api/tasks/{tid}/resume")
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        current = task(tid)
        if current["status"] == "COMPLETED":
            break
        if current["status"] == "FAILED":
            raise RuntimeError(f"File round failed: {current['checkpoint']}")
        approvals = [p for p in request("GET", "/api/approvals") if p["action"]["task_id"] == tid]
        for approval in approvals:
            spec = approval["action"]["spec"]
            assert spec["type"] == "files.write" and spec["path"] == "local-unified-proof.txt"
            assert "APPROVAL_REVISED" in spec["content"] and "blue" in spec["content"].lower()
            request(
                "POST",
                f"/api/approvals/{approval['id']}/approve",
                {"note": "isolated local acceptance file only"},
            )
        time.sleep(0.3)
    else:
        raise RuntimeError("Approval round timed out")
    completed = finish(tid)
    writes = [
        a for a in completed["actions"] if a["spec"]["type"] == "files.write" and a["status"] == "SUCCEEDED"
    ]
    assert len(writes) == 1
    assert "APPROVAL_REVISED" in writes[0]["spec"]["content"]
    report["stale_approval_denied_and_single_write"] = tid
    print("stale approval invalidation and actual approved write: passed", flush=True)

    projection = timeline(cid)
    artifact = next(a for a in projection["artifacts"] if a["path"] == "local-unified-proof.txt")
    content = client.get(f"/api/artifacts/{artifact['id']}/content")
    assert content.status_code == 200 and "APPROVAL_REVISED" in content.text
    report["downloaded_file_artifact"] = artifact["id"]
    assert len({i["id"] for i in projection["items"]}) == len(projection["items"])
    messages = request("GET", f"/api/conversations/{cid}/messages")
    inputs = [m for m in messages if m["role"] == "user"]
    assert all(m["delivered_at"] and m["submission_id"] for m in inputs)
    assert len({m["client_id"] for m in inputs}) == len(inputs)
    assert len(projection["tasks"]) == 3
    report["timeline_items"] = len(projection["items"])
    report["delivered_user_messages"] = len(inputs)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
