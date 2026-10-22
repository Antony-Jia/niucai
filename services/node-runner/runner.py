"""Linux node service. Owns one isolated Pi CLI process group, not model reasoning."""

import asyncio
import base64
import contextlib
import fcntl
import json
import os
import re
import signal
import tempfile
import time
from hashlib import sha256
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
        identity_file = self.root / "kernel-identity.json"
        self.identity_file = identity_file
        self.kernel_instance_id = os.environ.get("NIUCAI_KERNEL_INSTANCE_ID") or (
            json.loads(identity_file.read_text()).get("kernel_instance_id")
            if identity_file.exists()
            else None
        )
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
                                    "cost": {
                                        "input": 0,
                                        "output": 0,
                                        "cacheRead": 0,
                                        "cacheWrite": 0,
                                    },
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
        await self.post(
            f"/jobs/{record['id']}/finish",
            {"attempt_id": record["attempt_id"], **result},
        )
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
        outcome = {
            "status": "FAILED",
            "result": {"error": "Pi did not produce a final answer"},
        }
        last_lease = time.monotonic()
        lease_seconds = 5

        async def renew():
            nonlocal last_lease, lease_seconds
            while True:
                try:
                    await self.post(
                        "/heartbeat",
                        {
                            "executor": "pi",
                            "available": True,
                            "version": "1.1.0",
                            "protocol_version": 2,
                            "kernel_instance_id": self.kernel_instance_id,
                            "features": ["artifact-v2", "managed-workspace"],
                            "node_model": {"provider": self.provider, "model": self.model},
                        },
                    )
                    response = await self.post(base + "/heartbeat", {"attempt_id": attempt})
                    if response["stop"]:
                        stopped.set()
                        return
                    last_lease, lease_seconds = (
                        time.monotonic(),
                        response["lease_seconds"],
                    )
                except httpx.HTTPError:
                    # Stop well before the last acknowledged lease expires.
                    if time.monotonic() - last_lease >= max(1, lease_seconds - 6):
                        stopped.set()
                        return
                await asyncio.sleep(min(2, lease_seconds / 4))

        async def progress(kind, data):
            nonlocal sequence
            sequence += 1
            payload = {
                "attempt_id": attempt,
                "sequence": sequence,
                "kind": kind,
                "data": data,
            }
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
                    await progress(
                        "session",
                        {"session_id": frame.get("data", {}).get("sessionId", ident)},
                    )
                elif kind == "message_end" and frame.get("message", {}).get("role") == "assistant":
                    message = frame["message"]
                    failed = message.get("stopReason") in {"error", "aborted"}
                    answer = "\n".join(
                        b["text"] for b in message.get("content", []) if b.get("type") == "text"
                    )
                    await progress(
                        "assistant",
                        {
                            "text": answer[:4000],
                            "stop_reason": message.get("stopReason"),
                        },
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
            heartbeat = asyncio.create_task(renew())
            for reference in job.get("input_artifacts", []):
                if stopped.is_set():
                    raise RuntimeError("artifact download cancelled")
                if not re.fullmatch(r"[a-zA-Z0-9-]{1,36}", reference["id"]):
                    raise RuntimeError("invalid input artifact id")
                inputs = workspace / "inputs"
                inputs.mkdir(exist_ok=True)
                os.chown(inputs, self.uid, self.uid)
                path = inputs / reference["id"]
                digest, size = sha256(), 0
                async with self.client.stream(
                    "GET",
                    self.prefix + base + "/artifacts/" + reference["id"],
                    params={"attempt_id": attempt},
                ) as response:
                    response.raise_for_status()
                    with path.open("wb") as output:
                        async for content in response.aiter_bytes():
                            size += len(content)
                            if size > 20 * 1024 * 1024 or stopped.is_set():
                                raise RuntimeError("input artifact exceeds limit or cancelled")
                            digest.update(content)
                            output.write(content)
                if digest.hexdigest() != reference["content_hash"]:
                    path.unlink(missing_ok=True)
                    raise RuntimeError("input artifact hash mismatch")
                os.chown(path, self.uid, self.uid)
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
            process.stdin.write((json.dumps({"type": "get_state", "id": "state"}) + "\n").encode())
            process.stdin.write(
                (
                    json.dumps(
                        {
                            "type": "prompt",
                            "id": attempt,
                            "message": "Work in the assigned workspace. Return evidence and file outputs.\n"
                            + "Bot role and delivery configuration:\n"
                            + json.dumps(job.get("config_snapshot", {}), ensure_ascii=False)
                            + "\n"
                            + job["prompt"]
                            + (
                                "\nAuthorized input artifact IDs are materialized under inputs/. "
                                + json.dumps(job["input_artifacts"])
                                if job.get("input_artifacts")
                                else ""
                            ),
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
                    outcome = {
                        "status": "CANCELLED",
                        "result": {"error": "cancelled or lease revoked"},
                    }
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
            if outcome["status"] == "COMPLETED" and not job.get("artifact_transport"):
                # Standalone jobs and older Kernels only accept the bounded inline file result.
                outcome["result"]["files"] = self.collect_files(workspace)
            elif outcome["status"] == "COMPLETED":
                try:
                    outcome["result"]["files"] = await self.upload_files(workspace, base, attempt, stopped)
                except Exception as exc:
                    outcome = {
                        "status": "FAILED",
                        "result": {"error": f"artifact delivery failed: {type(exc).__name__}"},
                    }
            if heartbeat:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            record["finish"] = outcome
            atomic_json(self.ledger, record)
        await self.finish_ledger()

    async def upload_files(self, workspace, base, attempt, stopped):
        outputs, budget, visited = [], 100 * 1024 * 1024, 0
        for directory, dirs, files in os.walk(workspace, followlinks=False):
            dirs[:] = sorted(
                d
                for d in dirs
                if not d.startswith(".") and d != "inputs" and not (Path(directory) / d).is_symlink()
            )
            for name in sorted(files):
                visited += 1
                if visited > 256 or len(outputs) >= 16:
                    return outputs
                path = Path(directory) / name
                if name.startswith(".") or path.is_symlink() or not path.is_file():
                    continue
                if not path.resolve().is_relative_to(workspace.resolve()):
                    continue
                size = path.stat().st_size
                if size > 20 * 1024 * 1024 or size > budget:
                    raise RuntimeError("output file exceeds configured transport quota")
                relative = str(path.relative_to(workspace))
                filename = re.sub(r"[^a-zA-Z0-9._ -]", "_", name)[:150]
                filename = sha256(relative.encode()).hexdigest()[:12] + "-" + filename
                digest = sha256()
                with path.open("rb") as stream:
                    for content in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(content)
                upload = await self.post(
                    base + "/uploads",
                    {
                        "attempt_id": attempt,
                        "filename": filename,
                        "size": size,
                        "content_hash": digest.hexdigest(),
                        "media_type": "application/octet-stream",
                    },
                )
                offset = upload["received"]
                with path.open("rb") as stream:
                    stream.seek(offset)
                    while content := stream.read(upload["chunk_bytes"]):
                        if stopped.is_set():
                            raise RuntimeError("artifact upload cancelled")
                        response = await self.client.put(
                            self.prefix + base + "/uploads/" + upload["id"],
                            content=content,
                            params={"attempt_id": attempt, "offset": offset},
                        )
                        response.raise_for_status()
                        offset = response.json()["received"]
                ref = await self.post(
                    base + "/uploads/" + upload["id"] + "/finalize", {"attempt_id": attempt}
                )
                outputs.append({"path": relative, "bytes": size, "artifact": ref})
                budget -= size
        return outputs

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
                    content = path.read_bytes()
                    entry["base64"] = base64.b64encode(content).decode("ascii")
                    entry["content_hash"] = sha256(content).hexdigest()
                    try:
                        entry["text"] = content.decode("utf-8")
                        entry["media_type"] = "text/plain"
                    except UnicodeError:
                        entry["media_type"] = "application/octet-stream"
                    budget -= size
                except (UnicodeError, OSError):
                    pass
            outputs.append(entry)
        if len(json.dumps(outputs).encode()) > 180000:
            for entry in outputs:
                entry.pop("text", None)
            while outputs and len(json.dumps(outputs).encode()) > 180000:
                outputs.pop()
        return outputs

    async def run(self):
        async with self.client:
            while True:
                try:
                    response = await self.post(
                        "/heartbeat",
                        {
                            "executor": "pi",
                            "available": True,
                            "version": "1.1.0",
                            "protocol_version": 2,
                            "kernel_instance_id": self.kernel_instance_id,
                            "features": ["artifact-v2", "managed-workspace"],
                            "node_model": {"provider": self.provider, "model": self.model},
                        },
                    )
                    identity = response.get("kernel_instance_id")
                    if self.kernel_instance_id and identity != self.kernel_instance_id:
                        raise RuntimeError("KERNEL_IDENTITY_MISMATCH: refuse old ledger and new jobs")
                    if identity and not self.kernel_instance_id:
                        self.kernel_instance_id = identity
                        atomic_json(self.identity_file, {"kernel_instance_id": identity})
                    await self.finish_ledger()
                    if response["enabled"]:
                        job = await self.post("/claim")
                        if job:
                            print(f"Executing job {job['id']}", flush=True)
                            await self.execute(job)
                            continue
                except httpx.HTTPError as exc:
                    code = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                    print(
                        "TOKEN_INVALID" if code == 401 else f"KERNEL_UNREACHABLE: {type(exc).__name__}",
                        flush=True,
                    )
                except RuntimeError as exc:
                    print(str(exc), flush=True)
                await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.run(Runner().run())
