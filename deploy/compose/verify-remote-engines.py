"""Live Docker verification for engine dispatch (E2). Needs `remote-pi-demo.ps1 -Codex`.

Real Codex CLI in the node container, simulation Responses API as the model: this proves the
Runner/CLI/Kernel chain, not model quality. Reads local demo credentials without printing them.
"""

import json
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
codex_node = env["REMOTE_NODE_C_ID"]
evidence = {}


def request(path, body=None, method="POST"):
    response = client.request(method, path, json=body)
    response.raise_for_status()
    return response.json()


def wait(predicate, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = predicate()
        except httpx.TransportError:
            value = None
        if value:
            return value
        time.sleep(0.5)
    raise TimeoutError("live verification condition did not become true")


def submit(engine, allowed=None):
    value = request(
        "/api/remote/jobs",
        {
            "prompt": "Write proof.txt and provide evidence",
            "idempotency_key": f"engine-{time.time_ns()}-{engine}",
            "allowed_nodes": allowed or [],
            "engine": engine,
        },
    )
    request(f"/api/remote/jobs/{value['id']}/approve")
    return value["id"]


def finished(ident):
    value = request(f"/api/remote/jobs/{ident}", method="GET")
    return value if value["status"] in {"COMPLETED", "FAILED", "CANCELLED"} else None


def events(ident):
    return request(f"/api/remote/jobs/{ident}/events", method="GET")


nodes = {n["id"]: n for n in request("/api/remote/nodes", method="GET")}
assert nodes[codex_node]["engines"]["codex"]["available"], nodes[codex_node]
assert nodes[codex_node]["protocol_status"] == "COMPATIBLE"
evidence["codex_capabilities"] = nodes[codex_node]["engines"]["codex"]
print("PASS capabilities: protocol 3 node reports codex", evidence["codex_capabilities"])

# 1. A Codex job only runs on the Codex node and returns the file written by the real CLI.
codex_id = submit("codex")
codex_job = wait(lambda: finished(codex_id))
assert codex_job["status"] == "COMPLETED", codex_job
assert codex_job["node_id"] == codex_node, codex_job
result = codex_job["result"]
assert result["executor"] == "codex" and result["answer"].startswith("SIMULATION"), result
assert result["usage"]["input_tokens"] > 0 and codex_job["session_id"], codex_job
proof = [f for f in result["files"] if f["path"] == "proof.txt"]
assert proof and proof[0].get("text", "").strip() == "codex-simulation", result["files"]
kinds = [(e["data"]["kind"], e["data"]["payload"]) for e in events(codex_id) if e["data"].get("kind")]
assert all(data.get("engine") == "codex" for _, data in kinds), kinds
assert [k for k, _ in kinds if k.startswith("tool_execution")] == [
    "tool_execution_start",
    "tool_execution_end",
], kinds
assert "echo" not in json.dumps(kinds), "command text must not be reported"
evidence["codex_job"] = {"status": codex_job["status"], "node": "codex", "events": [k for k, _ in kinds]}
print("PASS codex job: completed on the Codex node with proof.txt, usage and engine-tagged events")

# 2. Pi jobs still run, and never need the Codex engine.
pi_id = submit("pi")
pi_job = wait(lambda: finished(pi_id))
assert pi_job["status"] == "COMPLETED" and pi_job["result"]["executor"] == "pi", pi_job
print("PASS pi job: still completes through Pi")

# 3. A Codex job pinned to a Pi-only node is never claimed and shows why it waits.
node_a = next(n["id"] for n in nodes.values() if n["name"].endswith(" A"))
stuck = submit("codex", [node_a])
time.sleep(6)
waiting = request(f"/api/remote/jobs/{stuck}", method="GET")
assert waiting["status"] == "QUEUED" and waiting["wait_state"]["reason"] == "ENGINE_NOT_READY", waiting
request(f"/api/remote/jobs/{stuck}/cancel")
print("PASS dispatch: Codex job pinned to a Pi-only node waits with ENGINE_NOT_READY")

out = root / "build-cache/remote-pi/verification-engines.json"
out.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
print("Evidence:", out)
