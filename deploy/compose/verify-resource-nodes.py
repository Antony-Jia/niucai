"""Isolated Docker resource acceptance; deterministic model, real Pi CLI and Runner."""

import json
import secrets
import subprocess
import time
from pathlib import Path

import httpx

root = Path(__file__).resolve().parents[2]
directory = root / "build-cache/resource-nodes"
directory.mkdir(parents=True, exist_ok=True)
env_path = directory / ".env"
if env_path.exists():
    env = dict(line.split("=", 1) for line in env_path.read_text().splitlines() if "=" in line)
else:
    env = {
        "REMOTE_DB_PASSWORD": secrets.token_hex(24),
        "REMOTE_API_TOKEN": secrets.token_hex(24),
        "REMOTE_API_PORT": "38182",
    }
env.update(REMOTE_MULTI_BOT_ENABLED="false", REMOTE_WORKER_SLOTS="1")


def save_env():
    env_path.write_text("".join(f"{key}={value}\n" for key, value in sorted(env.items())), encoding="utf-8")


def compose(*args):
    # Suppress configuration and container environment output (node credentials).
    subprocess.run(
        [
            "docker",
            "compose",
            "-p",
            "niucai-resource-test",
            "--env-file",
            str(env_path),
            "-f",
            str(root / "deploy/compose/compose.remote-pi.yml"),
            *args,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )


save_env()
# Compose interpolates even services not started yet.
for suffix in ("A", "B"):
    env.setdefault(f"REMOTE_NODE_{suffix}_ID", "pending")
    env.setdefault(f"REMOTE_NODE_{suffix}_TOKEN", "pending")
save_env()
compose("build", "api", "node-a")
compose("up", "-d", "--wait", "api", "model", "worker")
client = httpx.Client(
    base_url="http://127.0.0.1:38182",
    headers={"Authorization": f"Bearer {env['REMOTE_API_TOKEN']}"},
    timeout=10,
)


def request(path, body=None, method="POST"):
    response = client.request(method, path, json=body)
    response.raise_for_status()
    return response.json()


def nodes():
    return request("/api/remote/nodes", method="GET")


for suffix in ("A", "B"):
    if not any(node["id"] == env[f"REMOTE_NODE_{suffix}_ID"] for node in nodes()):
        node = request(
            "/api/remote/nodes", {"name": f"Resource Docker {suffix}", "resource_type": "local_docker"}
        )
        env[f"REMOTE_NODE_{suffix}_ID"], env[f"REMOTE_NODE_{suffix}_TOKEN"] = node["id"], node["token"]
save_env()
compose("up", "-d", "--wait", "node-a", "node-b")


def wait(predicate, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.3)
    raise TimeoutError("resource verification timed out")


def get(ident):
    return request(f"/api/remote/jobs/{ident}", method="GET")


def submit(key, allowed=()):
    value = request(
        "/api/remote/jobs",
        {
            "prompt": "Write proof.txt and provide evidence",
            "idempotency_key": f"resource-{time.time_ns()}-{key}",
            "allowed_nodes": list(allowed),
        },
    )
    request(f"/api/remote/jobs/{value['id']}/approve")
    return value["id"]


wait(lambda: len([node for node in nodes() if node["connection_status"] == "ONLINE"]) == 2)
ids = [submit(str(index)) for index in range(3)]


def parallel():
    jobs = [get(ident) for ident in ids]
    return (
        jobs
        if sum(j["status"] == "RUNNING" for j in jobs) == 2
        and sum(j["status"] == "QUEUED" for j in jobs) == 1
        else None
    )


evidence = {"model": "deterministic simulation; not real model or VPS acceptance", "parallel": wait(parallel)}
a_id = env["REMOTE_NODE_A_ID"]
a_path = f"/api/remote/nodes/{a_id}"
assert client.patch(a_path, json={"enabled": False}).status_code == 409
assert client.post(a_path + "/rotate-token").status_code == 409
wait(lambda: all(get(ident)["status"] == "COMPLETED" for ident in ids))
evidence["completed"] = [get(ident) for ident in ids]
print("PASS: two real Pi nodes run in parallel, third queues; occupied management rejected", flush=True)

request(a_path, {"enabled": False, "name": "Resource Docker A renamed"}, "PATCH")
view = next(node for node in nodes() if node["id"] == a_id)
assert view["connection_status"] == "ONLINE" and not view["enabled"]
blocked = submit("disabled", [a_id])
time.sleep(3)
assert get(blocked)["status"] == "QUEUED"
request(a_path, {"enabled": True}, "PATCH")
wait(lambda: get(blocked)["status"] == "COMPLETED")
evidence["disable_enable"] = {"online_while_disabled": True, "affinity_preserved": True}
print("PASS: disable preserves connectivity and queues pinned work; enable resumes scheduling", flush=True)

old_token = env["REMOTE_NODE_A_TOKEN"]
rotated = request(a_path + "/rotate-token")
assert rotated["connection_status"] == "PENDING"
response = httpx.post(
    f"http://127.0.0.1:38182/api/remote/runner/{a_id}/claim", headers={"Authorization": f"Bearer {old_token}"}
)
assert response.status_code == 401
env["REMOTE_NODE_A_TOKEN"] = rotated["token"]
save_env()
compose("up", "-d", "--force-recreate", "node-a")
wait(lambda: next(node for node in nodes() if node["id"] == a_id)["connection_status"] == "ONLINE")
reconnected = submit("reconnected", [a_id])
wait(lambda: get(reconnected)["status"] == "COMPLETED")
evidence["rotation"] = {"old_token_status": response.status_code, "reconnected_job": get(reconnected)}
print("PASS: old credential revoked; same node and data volume reconnect and execute", flush=True)

lost = submit("lost", [a_id])
wait(lambda: get(lost)["status"] == "RUNNING")
compose("stop", "node-a")
wait(lambda: get(lost)["status"] == "LOST", timeout=40)
assert client.patch(a_path, json={"enabled": False}).status_code == 409
assert client.post(a_path + "/rotate-token").status_code == 409
assert client.post(f"/api/remote/jobs/{lost}/retry").status_code == 409
compose("start", "node-a")
wait(lambda: get(lost)["status"] == "FAILED")
evidence["lost_recovery"] = get(lost)
print("PASS: LOST retains its slot and blocks management; restart confirms old process exit", flush=True)

# Real PostgreSQL lock tests use an additional empty test database in this isolated project.
subprocess.run(
    [
        "docker",
        "compose",
        "-p",
        "niucai-resource-test",
        "--env-file",
        str(env_path),
        "-f",
        str(root / "deploy/compose/compose.remote-pi.yml"),
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        "niucai",
        "-c",
        "CREATE DATABASE resource_test",
    ],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
(directory / "verification.json").write_text(
    json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
)
print("Evidence: build-cache/resource-nodes/verification.json", flush=True)
client.close()
