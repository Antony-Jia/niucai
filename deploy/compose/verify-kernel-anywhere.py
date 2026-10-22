"""Two real Linux Runner containers, simulated model; sanitize evidence before saving."""

import json
import time
from hashlib import sha256
from pathlib import Path

import httpx


def main():
    root = Path(__file__).resolve().parents[2]
    directory = root / "build-cache/niucai-anywhere-test"
    env = dict(
        line.split("=", 1)
        for line in (directory / ".env").read_text(encoding="utf-8-sig").splitlines()
        if "=" in line
    )
    with httpx.Client(
        base_url=f"http://127.0.0.1:{env['REMOTE_API_PORT']}",
        headers={"Authorization": f"Bearer {env['REMOTE_API_TOKEN']}"},
        timeout=20,
    ) as client:

        def request(path, body=None, method="GET"):
            response = client.request(method, path, json=body)
            response.raise_for_status()
            return response.json()

        identity = request("/api/kernel/identity")
        nodes = request("/api/remote/nodes")
        assert len(nodes) == 2 and all(n["connection_status"] == "ONLINE" for n in nodes)
        assert all(n["capabilities"]["protocol_version"] == 2 for n in nodes)
        existing = {b["id"] for b in request("/api/agents")}
        for index, ident in enumerate(["anywhere-dev", "anywhere-verify"]):
            body = {
                "name": ident,
                "definition": {
                    "tools": ["*"],
                    "instructions": f"Role rules for {ident}",
                    "allowed_nodes": [nodes[index]["id"]],
                    "resource_policy": {"strategy": "fixed", "fixed_node_id": nodes[index]["id"]},
                    "output_contract": {"require_text": True, "min_files": 1},
                },
            }
            request(
                f"/api/agents/{ident}" if ident in existing else "/api/agents",
                body if ident in existing else {"id": ident, **body},
                "PATCH" if ident in existing else "POST",
            )
        evidence = {"identity": identity, "provider": "deterministic; real Pi CLI and files", "runs": []}
        for _ in range(2):
            workflow = request(
                "/api/workflows",
                {
                    "goal": "ANYWHERE-MAIN develop then verify file evidence",
                    "participant_agent_ids": ["anywhere-dev", "anywhere-verify"],
                },
                "POST",
            )
            deadline = time.monotonic() + 240
            approvals, tasks_seen = [], set()
            while time.monotonic() < deadline:
                workflow = request(f"/api/workflows/{workflow['id']}")
                jobs = []
                for item in workflow["work_items"]:
                    tasks_seen.add(item["task_id"])
                    for job in request(f"/api/remote/jobs?parent_task_id={item['task_id']}"):
                        jobs.append(job)
                        if job["status"] == "WAITING_APPROVAL":
                            request(f"/api/remote/jobs/{job['id']}/approve", {}, "POST")
                            approvals.append(job["id"])
                    task = request(f"/api/tasks/{item['task_id']}")
                    if task["status"] == "WAITING_HUMAN" and any(
                        j["parent_task_id"] == task["id"] and j["status"] != "WAITING_APPROVAL" for j in jobs
                    ):
                        request(f"/api/tasks/{task['id']}/resume", {}, "POST")
                if workflow["status"] in {"COMPLETED", "FAILED", "CANCELLED"}:
                    break
                time.sleep(0.5)
            assert workflow["status"] == "COMPLETED", json.dumps(workflow)
            assert len(workflow["work_items"]) == 3
            assert len(jobs) == 2 and all(j["status"] == "COMPLETED" for j in jobs)
            for job in jobs:
                item = next(i for i in workflow["work_items"] if i["task_id"] == job["parent_task_id"])
                assert job["node_id"] == item["definition_snapshot"]["resource_policy"]["fixed_node_id"]
                assert job["config_snapshot"]["instructions"] == item["definition_snapshot"]["instructions"]
            developer = next(j for j in jobs if not j["input_artifacts"])
            verifier = next(j for j in jobs if j["input_artifacts"])
            assert developer["result"]["artifacts"] == verifier["input_artifacts"]
            artifact = verifier["input_artifacts"][0]
            content = client.get(f"/api/artifacts/{artifact['id']}/content")
            content.raise_for_status()
            assert (
                len(content.content) == 131072
                and sha256(content.content).hexdigest() == artifact["content_hash"]
            )
            evidence["runs"].append(
                {
                    "workflow": workflow,
                    "jobs": jobs,
                    "approved_jobs": approvals,
                    "handoffs": request(f"/api/handoffs?workflow_id={workflow['id']}"),
                }
            )
        evidence["diagnostics"] = request("/api/kernel/diagnostics")
        evidence["limitations"] = [
            "Both nodes are local containers",
            "Real VPS, real model quality and migration rehearsal remain unverified",
        ]
        output = directory / "verification.json"
        output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Verified two workflows, fixed nodes, manual approvals and 128 KiB handoff: {output}")


if __name__ == "__main__":
    main()
