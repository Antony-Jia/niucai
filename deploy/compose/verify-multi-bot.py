"""Live isolated demo verification; credentials remain in the local demo .env."""

import json
import argparse
import time
from pathlib import Path

import httpx


def main():
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-directory", type=Path, default=root / "build-cache/remote-pi")
    args = parser.parse_args()
    env = dict(
        line.split("=", 1)
        for line in (args.env_directory / ".env").read_text(encoding="utf-8-sig").splitlines()
        if "=" in line
    )
    with httpx.Client(
        base_url=f"http://127.0.0.1:{env['REMOTE_API_PORT']}",
        headers={"Authorization": f"Bearer {env['REMOTE_API_TOKEN']}"},
        timeout=15,
    ) as client:

        def request(path, body=None, method="GET"):
            response = client.request(method, path, json=body)
            response.raise_for_status()
            return response.json()

        def wait_workflow(ident):
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                workflow = request(f"/api/workflows/{ident}")
                if workflow["status"] in {"COMPLETED", "FAILED", "CANCELLED"}:
                    assert workflow["status"] == "COMPLETED", workflow
                    return workflow
                time.sleep(0.3)
            raise TimeoutError(f"workflow {ident} did not complete")

        status = request("/api/collaboration/status")
        assert status["enabled"] and not status["stopped"], "start remote-pi-demo.ps1 -MultiBot"
        existing = {bot["id"] for bot in request("/api/agents")}
        for ident in ["demo-research", "demo-writer", "demo-reviewer"]:
            body = {
                "name": ident,
                "definition": {
                    "tools": ["bot_send", "bot_wait", "bot_reply"],
                    "instructions": "Deterministic simulation Bot",
                },
            }
            if ident in existing:
                request(f"/api/agents/{ident}", body, "PATCH")
            else:
                request("/api/agents", {"id": ident, **body}, "POST")
        parallel = request(
            "/api/workflows",
            {
                "agent_id": "demo-research",
                "goal": "MULTIBOT-ROOT: delegate two results and aggregate",
            },
            "POST",
        )
        parallel = wait_workflow(parallel["id"])
        assert len(parallel["work_items"]) == 3
        stages = request(
            "/api/workflows",
            {
                "agent_id": "demo-research",
                "goal": "MULTIBOT-STAGE:research",
                "routes": [
                    ["demo-research", "demo-writer"],
                    ["demo-writer", "demo-reviewer"],
                    ["demo-reviewer", "demo-writer"],
                ],
                "final_agent_ids": ["demo-reviewer"],
                "max_revisions": 2,
            },
            "POST",
        )
        stages = wait_workflow(stages["id"])
        assert [
            item["assignee_agent_id"]
            for item in sorted(stages["work_items"], key=lambda item: item["created_at"])
        ] == [
            "demo-research",
            "demo-writer",
            "demo-reviewer",
            "demo-writer",
            "demo-reviewer",
        ]
        evidence = {
            "model": "deterministic Docker provider; no real model quality claim",
            "status": status,
            "workflows": [],
            "unverified": ["hard-kill recovery", "live provider quality"],
        }
        for workflow in [parallel, stages]:
            tasks = [request(f"/api/tasks/{item['task_id']}") for item in workflow["work_items"]]
            artifacts = [
                artifact for task in tasks for artifact in request(f"/api/tasks/{task['id']}/artifacts")
            ]
            evidence["workflows"].append(
                {
                    "workflow": workflow,
                    "tasks": tasks,
                    "artifacts": artifacts,
                    "handoffs": request(f"/api/handoffs?workflow_id={workflow['id']}"),
                    "remote_job_ids": [],
                }
            )
        output = (
            root / "build-cache/multi-bot/verification.json"
            if args.env_directory == root / "build-cache/remote-pi"
            else args.env_directory / "parallel-verification.json"
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Verified delegation and revision route: {output}")


if __name__ == "__main__":
    main()
