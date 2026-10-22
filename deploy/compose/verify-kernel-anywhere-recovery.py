"""Restart only this request's isolated test containers, verify durable history and hashes."""

import json
import subprocess
import time
from hashlib import sha256
from pathlib import Path

import httpx


def main():
    root = Path(__file__).resolve().parents[2]
    directory = root / "build-cache/niucai-anywhere-test"
    evidence = json.loads((directory / "verification.json").read_text(encoding="utf-8"))
    env = dict(
        line.split("=", 1) for line in (directory / ".env").read_text(encoding="utf-8-sig").splitlines()
    )
    with httpx.Client(
        base_url=f"http://127.0.0.1:{env['REMOTE_API_PORT']}",
        headers={"Authorization": f"Bearer {env['REMOTE_API_TOKEN']}"},
        timeout=10,
    ) as client:
        before = client.get("/api/kernel/identity").json()
        containers = [
            f"niucai-anywhere-test-{service}-1" for service in ["api", "worker", "node-a", "node-b"]
        ]
        for container in containers:
            label = subprocess.check_output(
                [
                    "docker",
                    "inspect",
                    "--format",
                    '{{index .Config.Labels "com.docker.compose.project"}}',
                    container,
                ],
                text=True,
            ).strip()
            if label != "niucai-anywhere-test":
                raise RuntimeError("refuse to restart unrelated container")
        subprocess.run(["docker", "restart", *containers], check=True, capture_output=True)
        deadline = time.monotonic() + 60
        after = None
        while time.monotonic() < deadline:
            try:
                response = client.get("/api/kernel/diagnostics")
                if response.status_code == 200 and response.json().get("execution_ready"):
                    after = response.json()
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        assert after and after["kernel_instance_id"] == before["kernel_instance_id"]
        hashes = []
        for run in evidence["runs"]:
            workflow = client.get(f"/api/workflows/{run['workflow']['id']}").json()
            assert workflow["status"] == "COMPLETED"
            for job in run["jobs"]:
                stored = client.get(f"/api/remote/jobs/{job['id']}").json()
                assert stored["status"] == "COMPLETED" and stored["attempt_id"] == job["attempt_id"]
                for artifact in stored["result"]["artifacts"]:
                    response = client.get(f"/api/artifacts/{artifact['id']}/content")
                    response.raise_for_status()
                    digest = sha256(response.content).hexdigest()
                    assert digest == artifact["content_hash"]
                    hashes.append({"id": artifact["id"], "sha256": digest})
        result = {
            "before": before,
            "after": after,
            "artifacts": hashes,
            "verified": "test API/Worker/Runner restart preserved completed workflows, attempts and files",
            "not_verified": "in-flight SSH tunnel loss, real VPS cold migration, Desktop native lifecycle",
        }
        (directory / "restart-verification.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print("Verified restart persistence and execution readiness in isolated test project")


if __name__ == "__main__":
    main()
