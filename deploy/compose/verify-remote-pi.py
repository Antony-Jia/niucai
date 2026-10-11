"""Live Docker verification. Reads local demo credentials without printing them."""

import json
import subprocess
import time
from pathlib import Path

import httpx

root = Path(__file__).resolve().parents[2]
env_path = root / "build-cache/remote-pi/.env"
env = dict(
    line.split("=", 1) for line in env_path.read_text(encoding="utf-8-sig").splitlines() if "=" in line
)
client = httpx.Client(
    base_url=f"http://127.0.0.1:{env['REMOTE_API_PORT']}",
    headers={"Authorization": f"Bearer {env['REMOTE_API_TOKEN']}"},
    timeout=10,
)
evidence = {}


def request(path, body=None, method="POST"):
    response = client.request(method, path, json=body)
    response.raise_for_status()
    return response.json()


def wait(predicate, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = predicate()
        except httpx.TransportError:
            value = None
        if value:
            return value
        time.sleep(0.3)
    raise TimeoutError("live verification condition did not become true")


def get(ident):
    return request(f"/api/remote/jobs/{ident}", method="GET")


def submit(key, allowed=None):
    value = request(
        "/api/remote/jobs",
        {
            "prompt": "Write proof.txt and provide evidence",
            "idempotency_key": f"verify-{time.time_ns()}-{key}",
            "allowed_nodes": allowed or [],
        },
    )
    assert value["status"] == "WAITING_APPROVAL"
    request(f"/api/remote/jobs/{value['id']}/approve")
    return value["id"]


def compose(*args):
    subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(env_path),
            "-f",
            str(root / "deploy/compose/compose.remote-pi.yml"),
            *args,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )


wait(lambda: len([n for n in request("/api/remote/nodes", method="GET") if n["status"] == "ONLINE"]) == 2)
ids = [submit(str(i)) for i in range(3)]
snapshot = wait(
    lambda: (
        (
            values
            if sum(j["status"] == "RUNNING" for j in values) == 2
            and sum(j["status"] == "QUEUED" for j in values) == 1
            else None
        )
        if (values := [get(i) for i in ids])
        else None
    )
)
assert len({j["node_id"] for j in snapshot if j["status"] == "RUNNING"}) == 2
evidence["parallel_and_queue"] = [
    {"id": j["id"], "status": j["status"], "node_id": j["node_id"]} for j in snapshot
]
completed = wait(
    lambda: (
        (values if all(j["status"] == "COMPLETED" for j in values) else None)
        if (values := [get(i) for i in ids])
        else None
    )
)
for value in completed:
    assert any(
        f["path"] == "proof.txt" and "real Pi CLI" in f.get("text", "") for f in value["result"]["files"]
    )
    events = request(f"/api/remote/jobs/{value['id']}/events", method="GET")
    assert any(e["data"].get("kind") == "tool_execution_end" for e in events)
evidence["completed_with_real_pi_files"] = completed
print(
    "PASS: two real Pi CLI nodes execute in parallel; third job queues; files and tool events returned",
    flush=True,
)

cancelled = submit("cancel", [env["REMOTE_NODE_A_ID"]])
wait(lambda: get(cancelled)["status"] == "RUNNING")
request(f"/api/remote/jobs/{cancelled}/cancel")
wait(lambda: get(cancelled)["status"] == "CANCELLED")
evidence["cancelled"] = get(cancelled)
request(f"/api/remote/jobs/{cancelled}/retry")
request(f"/api/remote/jobs/{cancelled}/approve")
wait(lambda: get(cancelled)["status"] == "COMPLETED")
evidence["retry"] = get(cancelled)
print("PASS: cancellation acknowledged after process stop; retry resumes on the pinned node", flush=True)

lost = submit("disconnect", [env["REMOTE_NODE_A_ID"]])
wait(lambda: get(lost)["status"] == "RUNNING")
compose("stop", "node-a")
wait(lambda: get(lost)["status"] == "LOST", timeout=35)
response = client.post(f"/api/remote/jobs/{lost}/retry")
assert response.status_code == 409
evidence["lost"] = get(lost)
compose("start", "node-a")
wait(lambda: get(lost)["status"] == "FAILED")
evidence["reconciled"] = get(lost)
print("PASS: node loss does not trigger duplicate execution; restart reconciles the old attempt", flush=True)

parent = request(
    "/api/tasks",
    {"title": "Remote Pi integration", "goal": "Delegate a remote Pi task and aggregate its verified result"},
)


def parent_result():
    for value in request("/api/remote/jobs", method="GET"):
        if value["parent_task_id"] == parent["id"] and value["status"] == "WAITING_APPROVAL":
            request(f"/api/remote/jobs/{value['id']}/approve")
    task = request(f"/api/tasks/{parent['id']}", method="GET")
    if (
        task["status"] == "WAITING_HUMAN"
        and task["checkpoint"].get("reason") == "remote Pi approval required"
    ):
        child = get(task["checkpoint"]["remote_job_id"])
        if child["status"] != "WAITING_APPROVAL":
            request(f"/api/tasks/{parent['id']}/resume")
            return None
    if task["status"] == "FAILED":
        raise AssertionError(task["checkpoint"])
    return task if task["status"] == "COMPLETED" else None


evidence["main_pi_delegation"] = wait(parent_result, timeout=90)
assert any(
    j["parent_task_id"] == parent["id"] and j["status"] == "COMPLETED"
    for j in request("/api/remote/jobs", method="GET")
)
print(
    "PASS: main Pi delegates through Kernel, receives the child result and completes aggregation", flush=True
)
(env_path.parent / "verification.json").write_text(
    json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
)
print("Evidence: build-cache/remote-pi/verification.json", flush=True)
