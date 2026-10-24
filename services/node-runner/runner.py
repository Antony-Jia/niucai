"""Linux node service. Owns one isolated engine CLI process group, not model reasoning."""

import asyncio
import base64
import contextlib
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
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


class PiEngine:
    """Pi RPC engine: the only engine today. Owns its Unix UID, HOME and CLI protocol."""

    name = "pi"
    label = "Pi"
    closes_stdin = False  # RPC stays open until the process is killed

    def __init__(self, root):
        self.uid = int(os.environ.get("NIUCAI_PI_UID", "10001"))
        self.home = root / "pi-home"
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

    def heartbeat(self):
        return {
            "executor": self.name,
            "available": True,
            "version": "1.1.0",
            "features": ["artifact-v2", "managed-workspace"],
            "node_model": {"provider": self.provider, "model": self.model},
        }

    def status(self):
        return {"available": True, "version": "1.1.0", "model": self.model}

    def environment(self):
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

    def command(self, session, job=None):
        return [
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

    @staticmethod
    def stdin(attempt, message):
        frames = (
            {"type": "get_state", "id": "state"},
            {"type": "prompt", "id": attempt, "message": message},
        )
        return "".join(json.dumps(frame) + "\n" for frame in frames).encode()

    def translate(self, frame, state):
        """Map one raw frame to (events, final). `final` is the result dict when finished."""
        kind = frame.get("type", "")
        if kind == "response" and not frame.get("success", True):
            raise RuntimeError(str(frame.get("error", "Pi RPC command rejected"))[:500])
        if kind == "response" and frame.get("command") == "get_state":
            data = {"session_id": frame.get("data", {}).get("sessionId", state["job_id"])}
            return [("session", data)], None
        if kind == "message_end" and frame.get("message", {}).get("role") == "assistant":
            message = frame["message"]
            state["failed"] = message.get("stopReason") in {"error", "aborted"}
            state["answer"] = "\n".join(
                b["text"] for b in message.get("content", []) if b.get("type") == "text"
            )
            data = {"text": state["answer"][:4000], "stop_reason": message.get("stopReason")}
            return [("assistant", data)], None
        if kind in {"tool_execution_start", "tool_execution_end"}:
            data = {
                "tool": str(frame.get("toolName", ""))[:100],
                "is_error": bool(frame.get("isError")),
            }
            return [(kind, data)], None
        if kind in {"agent_end", "agent_settled"}:
            if not state["answer"] or state["failed"]:
                raise RuntimeError("Pi ended without a successful final answer")
            return [], {
                "answer": state["answer"][:16000],
                "executor": self.name,
                "session_path": state["session_path"],
            }
        return [], None


class CodexEngine:
    """Codex CLI engine: `codex exec --json -`, prompt on stdin, API-key auth only."""

    name = "codex"
    label = "Codex"
    closes_stdin = True  # `exec -` reads the prompt until EOF
    sandboxes = {"read": "read-only", "workspace": "workspace-write", "full": "danger-full-access"}
    ignored_items = {"reasoning", "todo_list", "agent_message", "error"}  # item "error" = CLI warning
    tools = {
        "command_execution": "shell",
        "file_change": "edit",
        "mcp_tool_call": "mcp",
        "web_search": "web_search",
    }

    def __init__(self, root):
        self.uid = int(os.environ.get("NIUCAI_CODEX_UID", "10002"))
        self.home = root / "codex-home"
        self.home.mkdir(exist_ok=True)
        os.chown(self.home, self.uid, self.uid)
        self.model = os.environ.get("NIUCAI_CODEX_MODEL", "")
        self.base_url = os.environ.get("NIUCAI_CODEX_BASE_URL", "")
        allowed = {a.strip() for a in os.environ.get("NIUCAI_CODEX_PROFILES", "read,workspace").split(",")}
        self.version = self.probe_version()
        # Codex's own sandbox needs unprivileged user namespaces, which hardened containers deny.
        self.sandbox = self.probe_sandbox()
        self.profiles = [
            p
            for p in ("read", "workspace", "full")
            if p in allowed and (self.sandbox == "engine" or p != "read")  # read-only cannot be enforced
        ]

    @staticmethod
    def probe_version():
        if not shutil.which("codex"):
            return ""
        try:
            output = subprocess.run(
                ["codex", "--version"], capture_output=True, text=True, timeout=10, check=False
            ).stdout.split()
        except (OSError, subprocess.SubprocessError):
            return ""
        return output[-1][:100] if output else ""

    def probe_sandbox(self):
        if not shutil.which("codex"):
            return "container"
        try:
            probe = subprocess.run(
                ["codex", "sandbox", "--", "/bin/true"],
                capture_output=True,
                timeout=30,
                check=False,
                cwd=self.home,
                env=self.environment(),
                preexec_fn=self.drop_privileges,
            )
        except (OSError, subprocess.SubprocessError):
            return "container"
        return "engine" if probe.returncode == 0 else "container"

    @staticmethod
    def credential():
        return os.environ.get("NIUCAI_CODEX_API_KEY", "")

    def status(self):
        if not shutil.which("codex"):
            return {"available": False, "reason": "not_installed"}
        if not self.credential():
            return {"available": False, "reason": "credential_missing"}
        return {
            "available": True,
            "version": self.version,
            "model": self.model,
            "profiles": self.profiles,
            "sandbox": self.sandbox,
        }

    def environment(self):
        # Only Codex's own credential; the node token and Pi keys never reach this process.
        return {
            "PATH": os.environ["PATH"],
            "HOME": str(self.home),
            "CODEX_HOME": str(self.home),
            "LANG": "C.UTF-8",
            "CODEX_API_KEY": self.credential(),
        }

    def drop_privileges(self):
        os.setgroups([])
        os.setgid(self.uid)
        os.setuid(self.uid)

    def command(self, session, job=None):
        profile = ((job or {}).get("config_snapshot") or {}).get("permission_profile") or "workspace"
        if profile not in self.sandboxes or profile not in self.profiles:
            raise ValueError(f"permission profile not enabled on this node: {str(profile)[:40]}")
        args = [
            "codex",
            "exec",
            "--json",
            "--skip-git-repo-check",
            "--sandbox",
            # Without the engine sandbox the container is the boundary; `workspace` is labelled as such.
            self.sandboxes[profile] if self.sandbox == "engine" else "danger-full-access",
            "--output-last-message",
            str(session.with_suffix(".txt")),
            "-c",
            'approval_policy="never"',
            "-c",
            "project_doc_max_bytes=0",  # workspace AGENTS.md is untrusted input
            "-c",
            'web_search="disabled"',
        ]
        if self.model:
            args += ["-m", self.model]
        if self.base_url:
            provider = "model_providers.niucai"
            args += [
                "-c",
                'model_provider="niucai"',
                "-c",
                f'{provider}.name="niucai"',
                "-c",
                f"{provider}.base_url=" + json.dumps(self.base_url),
                "-c",
                f'{provider}.env_key="CODEX_API_KEY"',
                "-c",
                f'{provider}.wire_api="responses"',
            ]
        return [*args, "-"]

    @staticmethod
    def stdin(attempt, message):
        return message.encode()

    @staticmethod
    def tool_failed(item):
        status = item.get("status")
        if item.get("type") == "command_execution":
            return item.get("exit_code") not in (0, None) or status == "failed"
        if item.get("type") == "mcp_tool_call":
            return status == "failed" or bool(item.get("error"))
        return status == "failed"

    def translate(self, frame, state):
        """Map one `codex exec --json` frame to (events, final), like PiEngine.translate."""
        kind = frame.get("type", "")
        if kind == "thread.started":
            state["thread_id"] = str(frame.get("thread_id", ""))
            return [("session", {"session_id": state["thread_id"] or state["job_id"]})], None
        if kind in {"item.started", "item.updated", "item.completed"}:
            item = frame.get("item") or {}
            item_type = item.get("type", "")
            if item_type == "agent_message" and kind == "item.completed":
                state["answer"] = str(item.get("text", ""))
                data = {"text": state["answer"][:4000], "stop_reason": "stop"}
                return [("assistant", data)], None
            tool = self.tools.get(item_type)
            if tool is None:
                if item_type not in self.ignored_items:
                    state["unknown_frames"] = state.get("unknown_frames", 0) + 1
                return [], None
            opened = state.setdefault("open", set())
            events = []
            if item.get("id") not in opened and kind != "item.updated":
                opened.add(item.get("id"))
                events.append(("tool_execution_start", {"tool": tool, "is_error": False}))
            if kind == "item.completed":
                opened.discard(item.get("id"))
                events.append(("tool_execution_end", {"tool": tool, "is_error": self.tool_failed(item)}))
            return events, None
        if kind == "turn.failed":
            raise RuntimeError(str((frame.get("error") or {}).get("message") or "Codex turn failed")[:500])
        if kind == "error":
            message = str(frame.get("message", ""))
            if message.startswith("Reconnecting"):  # transient stream retry, not a failure
                return [], None
            raise RuntimeError(message[:500] or "Codex error")
        if kind == "turn.completed":
            answer = state["answer"]
            if not answer:
                with contextlib.suppress(OSError):
                    answer = Path(state["session_path"]).with_suffix(".txt").read_text()
            if not answer:
                raise RuntimeError("Codex ended without a final answer")
            final = {
                "answer": answer[:16000],
                "executor": self.name,
                "session_id": state.get("thread_id", ""),
            }
            if isinstance(frame.get("usage"), dict):
                final["usage"] = {k: v for k, v in frame["usage"].items() if isinstance(v, int)}
            if state.get("unknown_frames"):
                final["unknown_frames"] = state["unknown_frames"]
            return [], final
        if kind != "turn.started":
            state["unknown_frames"] = state.get("unknown_frames", 0) + 1
        return [], None


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
        self.requested = {e.strip() for e in os.environ.get("NIUCAI_ENGINES", "pi").split(",")}
        engines = [PiEngine(self.root)]
        if "codex" in self.requested:
            engines.append(CodexEngine(self.root))
        self.engines = {engine.name: engine for engine in engines}

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

    def heartbeat_payload(self):
        executors = {name: engine.status() for name, engine in self.engines.items()}
        if "claude" in self.requested:
            executors["claude"] = {"available": False, "reason": "not_implemented"}
        return {
            **self.engines["pi"].heartbeat(),
            "protocol_version": 3,
            "executors": executors,
            "kernel_instance_id": self.kernel_instance_id,
        }

    @staticmethod
    def compose_prompt(job):
        return (
            "Work in the assigned workspace. Return evidence and file outputs.\n"
            + "Bot role and delivery configuration:\n"
            + json.dumps(job.get("config_snapshot", {}), ensure_ascii=False)
            + "\n"
            + job["prompt"]
            + (
                "\nAuthorized input artifact IDs are materialized under inputs/. "
                + json.dumps(job["input_artifacts"])
                if job.get("input_artifacts")
                else ""
            )
        )

    async def execute(self, job):
        ident, attempt = job["id"], job["attempt_id"]
        base = f"/jobs/{ident}"
        record = {"id": ident, "attempt_id": attempt}
        atomic_json(self.ledger, record)  # Durable admission before starting a process.
        engine = self.engines.get(job.get("engine") or "pi")
        if engine is None or not engine.status()["available"]:
            record["finish"] = {
                "status": "FAILED",
                "result": {"error": f"engine not available on this node: {job.get('engine')}"[:500]},
            }
            atomic_json(self.ledger, record)
            await self.finish_ledger()
            return
        uid = engine.uid
        workspace = self.root / "workspaces" / ident
        sessions = self.root / "sessions"
        workspace.mkdir(parents=True, exist_ok=True)
        sessions.mkdir(exist_ok=True)
        os.chown(workspace, uid, uid)
        os.chown(sessions, uid, uid)
        session = sessions / f"{ident}.jsonl"
        process = None
        heartbeat = None
        stopped = asyncio.Event()
        sequence = 0
        outcome = {
            "status": "FAILED",
            "result": {"error": f"{engine.label} did not produce a final answer"},
        }
        last_lease = time.monotonic()
        lease_seconds = 5

        async def renew():
            nonlocal last_lease, lease_seconds
            while True:
                try:
                    await self.post("/heartbeat", self.heartbeat_payload())
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
                "data": {**data, "engine": engine.name},
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
            state = {
                "job_id": ident,
                "answer": "",
                "failed": False,
                "session_path": str(session),
            }
            while raw := await process.stdout.readline():
                events, final = engine.translate(json.loads(raw), state)
                for kind, data in events:
                    await progress(kind, data)
                if final is not None:
                    outcome = {"status": "COMPLETED", "result": final}
                    return
            raise RuntimeError(f"{engine.label} exited before completion")

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
                os.chown(inputs, uid, uid)
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
                os.chown(path, uid, uid)
            args = engine.command(session, job)
            process = await asyncio.create_subprocess_exec(
                *args,
                cwd=workspace,
                env=engine.environment(),
                start_new_session=True,
                preexec_fn=engine.drop_privileges,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=2 * 1024 * 1024,
            )
            record.update(pid=process.pid, process_start=process_start(process.pid))
            atomic_json(self.ledger, record)
            process.stdin.write(engine.stdin(attempt, self.compose_prompt(job)))
            await process.stdin.drain()
            if engine.closes_stdin:
                process.stdin.close()
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
                    raise TimeoutError(f"remote {engine.label} job execution timeout")
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
                    response = await self.post("/heartbeat", self.heartbeat_payload())
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
