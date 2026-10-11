"""Linux node service. Owns one isolated Pi CLI process group, not model reasoning."""

import asyncio
import contextlib
import fcntl
import json
import os
import signal
import tempfile
import time
from pathlib import Path

import httpx


def atomic_json(path, value, owner=None):
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=".runner-")
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
            if owner is not None:
                os.fchown(stream.fileno(), owner, owner)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def process_start(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except FileNotFoundError:
        return None


async def kill_group(pid):
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pid, signal.SIGTERM)
    await asyncio.sleep(0.3)
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pid, signal.SIGKILL)


class Runner:
    def __init__(self):
        self.root = Path(os.environ.get("NIUCAI_NODE_DATA", "/data"))
        self.control = self.root / "control"
        self.control.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.ledger = self.control / "active.json"
        self.lock = (self.control / "runner.lock").open("a+")
        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.node_id = os.environ["NIUCAI_NODE_ID"]
        self.prefix = f"/api/remote/runner/{self.node_id}"
        self.client = httpx.AsyncClient(
            base_url=os.environ["NIUCAI_KERNEL_URL"].rstrip("/"),
            headers={"Authorization": f"Bearer {os.environ['NIUCAI_NODE_TOKEN']}"},
            timeout=5,
        )
        self.uid = int(os.environ.get("NIUCAI_PI_UID", "10001"))
        self.home = self.root / "pi-home"
        self.home.mkdir(exist_ok=True)
        os.chown(self.home, self.uid, self.uid)
        self.provider = os.environ.get("NIUCAI_PI_PROVIDER", "openrouter")
        self.model = os.environ.get("NIUCAI_PI_MODEL", "")
        if not self.model:
            raise ValueError("NIUCAI_PI_MODEL must be configured")
        if os.environ.get("NIUCAI_PI_BASE_URL"):
            atomic_json(
                self.home / "models.json",
                {
                    "providers": {
                        self.provider: {
                            "baseUrl": os.environ["NIUCAI_PI_BASE_URL"],
                            "api": "openai-completions",
                            "apiKey": "${PI_PROVIDER_KEY}",
                            "models": [
                                {
                                    "id": self.model,
                                    "contextWindow": 64000,
                                    "maxTokens": 4096,
                                    "reasoning": False,
                                    "input": ["text"],
                                    "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                                }
                            ],
                        }
                    }
                },
                owner=self.uid,
            )

    async def post(self, suffix, data=None):
        response = await self.client.post(self.prefix + suffix, json=data)
        response.raise_for_status()
        return response.json()

    async def finish_ledger(self):
        if not self.ledger.exists():
            return
        record = json.loads(self.ledger.read_text())
        pid = record.get("pid")
        if pid and process_start(pid) == record.get("process_start"):
            await kill_group(pid)
        # Always reconcile the old attempt before claiming another job.
        result = record.get(
            "finish",
            {
                "status": "FAILED",
                "result": {
                    "error": "Runner restarted; previous process stopped; retry explicitly to resume session"
                },
            },
        )
        await self.post(f"/jobs/{record['id']}/finish", {"attempt_id": record["attempt_id"], **result})
        self.ledger.unlink()

    def pi_environment(self):
        # Runner credentials are never inherited by Pi; Pi runs under another Unix UID.
        result = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.home),
            "PI_CODING_AGENT_DIR": str(self.home),
            "LANG": "C.UTF-8",
            "PI_PROVIDER_KEY": os.environ.get("NIUCAI_PI_PROVIDER_KEY", ""),
        }
        for name in ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
            if name in os.environ:
                result[name] = os.environ[name]
        return result

    def drop_privileges(self):
        os.setgroups([])
        os.setgid(self.uid)
        os.setuid(self.uid)

    async def execute(self, job):
        ident, attempt = job["id"], job["attempt_id"]
        base = f"/jobs/{ident}"
        record = {"id": ident, "attempt_id": attempt}
        atomic_json(self.ledger, record)  # Durable admission before starting a process.
        workspace = self.root / "workspaces" / ident
        sessions = self.root / "sessions"
        workspace.mkdir(parents=True, exist_ok=True)
        sessions.mkdir(exist_ok=True)
        os.chown(workspace, self.uid, self.uid)
        os.chown(sessions, self.uid, self.uid)
        session = sessions / f"{ident}.jsonl"
        args = [
            "pi",
            "--mode",
            "rpc",
            "--provider",
            self.provider,
            "--model",
            self.model,
            "--session",
            str(session),
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-approve",
            "--no-context-files",
            "--no-mcp",
            "--offline",
        ]
        process = None
        heartbeat = None
        stopped = asyncio.Event()
        sequence = 0
        outcome = {"status": "FAILED", "result": {"error": "Pi did not produce a final answer"}}
        last_lease = time.monotonic()
        lease_seconds = 5

        async def renew():
            nonlocal last_lease, lease_seconds
            while True:
                try:
                    await self.post("/heartbeat", {"executor": "pi", "available": True, "version": "1.1.0"})
                    response = await self.post(base + "/heartbeat", {"attempt_id": attempt})
                    if response["stop"]:
                        stopped.set()
                        return
                    last_lease, lease_seconds = time.monotonic(), response["lease_seconds"]
                except httpx.HTTPError:
                    # Stop well before the last acknowledged lease expires.
                    if time.monotonic() - last_lease >= max(1, lease_seconds - 6):
                        stopped.set()
                        return
                await asyncio.sleep(min(2, lease_seconds / 4))

        async def progress(kind, data):
            nonlocal sequence
            sequence += 1
            payload = {"attempt_id": attempt, "sequence": sequence, "kind": kind, "data": data}
            for count in range(3):
                try:
                    await self.post(base + "/events", payload)
                    return
                except httpx.HTTPError:
                    if count == 2:
                        stopped.set()
                        raise
                    await asyncio.sleep(0.2)

        async def read_events():
            nonlocal outcome
            answer, failed = "", False
            while raw := await process.stdout.readline():
                frame = json.loads(raw)
                kind = frame.get("type", "")
                if kind == "response" and not frame.get("success", True):
                    raise RuntimeError(str(frame.get("error", "Pi RPC command rejected"))[:500])
                if kind == "response" and frame.get("command") == "get_state":
                    await progress("session", {"session_id": frame.get("data", {}).get("sessionId", ident)})
                elif kind == "message_end" and frame.get("message", {}).get("role") == "assistant":
                    message = frame["message"]
                    failed = message.get("stopReason") in {"error", "aborted"}
                    answer = "\n".join(
                        b["text"] for b in message.get("content", []) if b.get("type") == "text"
                    )
                    await progress(
                        "assistant", {"text": answer[:4000], "stop_reason": message.get("stopReason")}
                    )
                elif kind in {"tool_execution_start", "tool_execution_end"}:
                    await progress(
                        kind,
                        {
                            "tool": str(frame.get("toolName", ""))[:100],
                            "is_error": bool(frame.get("isError")),
                        },
                    )
                elif kind in {"agent_end", "agent_settled"}:
                    if not answer or failed:
                        raise RuntimeError("Pi ended without a successful final answer")
                    outcome = {
                        "status": "COMPLETED",
                        "result": {
                            "answer": answer[:16000],
                            "executor": "pi",
                            "session_path": str(session),
                        },
                    }
                    return
            raise RuntimeError("Pi exited before completion")

        try:
            first = await self.post(base + "/heartbeat", {"attempt_id": attempt})
            if first["stop"]:
                raise RuntimeError("execution permission revoked before launch")
            lease_seconds = first["lease_seconds"]
            process = await asyncio.create_subprocess_exec(
                *args,
                cwd=workspace,
                env=self.pi_environment(),
                start_new_session=True,
                preexec_fn=self.drop_privileges,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=2 * 1024 * 1024,
            )
            record.update(pid=process.pid, process_start=process_start(process.pid))
            atomic_json(self.ledger, record)
            heartbeat = asyncio.create_task(renew())
            process.stdin.write((json.dumps({"type": "get_state", "id": "state"}) + "\n").encode())
            process.stdin.write(
                (
                    json.dumps(
                        {
                            "type": "prompt",
                            "id": attempt,
                            "message": "Work in the assigned workspace. Return evidence and file outputs.\n"
                            + job["prompt"],
                        }
                    )
                    + "\n"
                ).encode()
            )
            await process.stdin.drain()
            reader = asyncio.create_task(read_events())
            stop_wait = asyncio.create_task(stopped.wait())
            try:
                done, _ = await asyncio.wait(
                    {reader, stop_wait},
                    timeout=float(os.environ.get("NIUCAI_PI_JOB_TIMEOUT", "1800")),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if reader in done:
                    await reader
                elif stopped.is_set():
                    outcome = {"status": "CANCELLED", "result": {"error": "cancelled or lease revoked"}}
                else:
                    raise TimeoutError("remote Pi job execution timeout")
            finally:
                reader.cancel()
                stop_wait.cancel()
                await asyncio.gather(reader, stop_wait, return_exceptions=True)
        except Exception as exc:
            outcome = {"status": "FAILED", "result": {"error": str(exc)[:500]}}
        finally:
            if process:
                # Even on success stop background descendants before releasing the slot.
                await kill_group(process.pid)
                await process.wait()
            if heartbeat:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            if outcome["status"] == "COMPLETED":
                outcome["result"]["files"] = self.collect_files(workspace)
            record["finish"] = outcome
            atomic_json(self.ledger, record)
        await self.finish_ledger()

    @staticmethod
    def collect_files(workspace):
        outputs, budget = [], 64000
        # Bound traversal as well as payload size; avoid following directory symlinks.
        candidates = []
        for directory, dirs, files in os.walk(workspace, followlinks=False):
            dirs[:] = sorted(
                d for d in dirs if not d.startswith(".") and not (Path(directory) / d).is_symlink()
            )
            candidates.extend(Path(directory) / name for name in sorted(files) if not name.startswith("."))
            if len(candidates) >= 256:
                break
        for path in candidates[:256]:
            if len(outputs) >= 16:
                break
            if (
                path.is_symlink()
                or not path.is_file()
                or not path.resolve().is_relative_to(workspace.resolve())
            ):
                continue
            if any(part.startswith(".") for part in path.relative_to(workspace).parts):
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            relative = str(path.relative_to(workspace))
            if len(relative) > 1024:
                continue
            entry = {"path": relative, "bytes": size}
            if size <= min(16000, budget):
                try:
                    entry["text"] = path.read_text(encoding="utf-8")
                    budget -= size
                except (UnicodeError, OSError):
                    pass
            outputs.append(entry)
        return outputs

    async def run(self):
        async with self.client:
            while True:
                try:
                    await self.finish_ledger()
                    response = await self.post(
                        "/heartbeat", {"executor": "pi", "available": True, "version": "1.1.0"}
                    )
                    if response["enabled"]:
                        job = await self.post("/claim")
                        if job:
                            print(f"Executing job {job['id']}", flush=True)
                            await self.execute(job)
                            continue
                except httpx.HTTPError as exc:
                    print(f"Kernel unavailable: {type(exc).__name__}", flush=True)
                await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.run(Runner().run())
