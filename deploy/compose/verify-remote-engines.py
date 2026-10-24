"""Live Docker verification for engine dispatch (E2/E3). Needs `remote-pi-demo.ps1 -Codex` and/or `-Claude`.

Real Codex / Claude Code CLIs in the node containers, simulation model APIs: this proves the
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
codex_node = env.get("REMOTE_NODE_C_ID", "")
claude_node = env.get("REMOTE_NODE_D_ID", "")
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


def submit(engine, allowed=None, profile=None):
    value = request(
        "/api/remote/jobs",
        {
            "prompt": "Write proof.txt and provide evidence",
            "idempotency_key": f"engine-{time.time_ns()}-{engine}",
            "allowed_nodes": allowed or [],
            "engine": engine,
            **({"permission_profile": profile} if profile else {}),
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
has_codex = codex_node in nodes and nodes[codex_node]["engines"].get("codex", {}).get("available")
has_claude = claude_node in nodes and nodes[claude_node]["engines"].get("claude", {}).get("available")
assert has_codex or has_claude, "start a node with `remote-pi-demo.ps1 -Codex` or `-Claude` first"
node_a = next(n["id"] for n in nodes.values() if n["name"].endswith(" A"))


def check_engine(engine, node, proof_text, answer_prefix):
    """The engine's job runs only on its node and returns the file written by the real CLI."""
    assert nodes[node]["protocol_status"] == "COMPATIBLE", nodes[node]
    evidence[f"{engine}_capabilities"] = nodes[node]["engines"][engine]
    print(f"PASS capabilities: protocol 3 node reports {engine}", evidence[f"{engine}_capabilities"])
    ident = submit(engine)
    job = wait(lambda: finished(ident))
    assert job["status"] == "COMPLETED", job
    assert job["node_id"] == node, job
    result = job["result"]
    assert result["executor"] == engine and result["answer"].startswith(answer_prefix), result
    assert result["usage"]["input_tokens"] > 0 and job["session_id"], job
    proof = [f for f in result["files"] if f["path"] == "proof.txt"]
    assert proof and proof[0].get("text", "").strip() == proof_text, result["files"]
    kinds = [(e["data"]["kind"], e["data"]["payload"]) for e in events(ident) if e["data"].get("kind")]
    assert all(data.get("engine") == engine for _, data in kinds), kinds
    assert [k for k, _ in kinds if k.startswith("tool_execution")] == [
        "tool_execution_start",
        "tool_execution_end",
    ], kinds
    assert "echo" not in json.dumps(kinds), "command text must not be reported"
    evidence[f"{engine}_job"] = {"status": job["status"], "events": [k for k, _ in kinds]}
    print(f"PASS {engine} job: completed on the {engine} node with proof.txt, usage and engine-tagged events")


if has_codex:
    check_engine("codex", codex_node, "codex-simulation", "SIMULATION")
if has_claude:
    check_engine("claude", claude_node, "claude-simulation", "SIMULATION")

# Permission profiles reach the real CLI: `read` removes write tools, `full` is not enabled on the node.
for engine, node in (("codex", codex_node), ("claude", claude_node)):
    if not nodes.get(node, {}).get("engines", {}).get(engine, {}).get("available"):
        continue
    offered = nodes[node]["engines"][engine].get("profiles", [])
    print(f"{engine} node offers profiles", offered)
    denied = submit(engine, [node], "full")
    job = wait(lambda: finished(denied))
    assert job["status"] == "FAILED" and job["config_snapshot"]["permission_profile"] == "full", job
    print(f"PASS profile: {engine} refuses `full`, which the node did not enable")
    if "read" in offered:
        ident = submit(engine, [node], "read")
        job = wait(lambda: finished(ident))
        files = [f["path"] for f in job["result"].get("files", [])] if job["status"] == "COMPLETED" else []
        assert "proof.txt" not in files, job
        print(f"PASS profile: {engine} `read` job ended {job['status']} without writing proof.txt")
        evidence[f"{engine}_read_profile"] = {"status": job["status"], "files": files}
    else:
        print(f"SKIP profile: {engine} node does not offer `read` in this container")

# Pi jobs still run, and never need another engine.
pi_id = submit("pi")
pi_job = wait(lambda: finished(pi_id))
assert pi_job["status"] == "COMPLETED" and pi_job["result"]["executor"] == "pi", pi_job
print("PASS pi job: still completes through Pi")

# An engine job pinned to a node without that engine is never claimed and shows why it waits.
for engine in [e for e, ok in (("codex", has_codex), ("claude", has_claude)) if ok]:
    stuck = submit(engine, [node_a])  # node A only has Pi
    time.sleep(6)
    waiting = request(f"/api/remote/jobs/{stuck}", method="GET")
    assert waiting["status"] == "QUEUED" and waiting["wait_state"]["reason"] == "ENGINE_NOT_READY", waiting
    request(f"/api/remote/jobs/{stuck}/cancel")
    print(f"PASS dispatch: {engine} job pinned to a node without it waits with ENGINE_NOT_READY")

out = root / "build-cache/remote-pi/verification-engines.json"
out.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
print("Evidence:", out)
