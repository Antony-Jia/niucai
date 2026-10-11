"""Pi Durable owns reasoning; Kernel journals and fences every external operation.

Private stdio transport deliberately keeps credentials and computer capabilities out
of the Node harness. JSONL sessions live outside the agent workspace, with fsync and
an inherited OS lock held until the child exits. No distributed transaction is claimed.
"""

import asyncio
import fcntl
import json
import os
import shutil
from hashlib import sha256

from pydantic import TypeAdapter
from sqlalchemy import select

from niucai.control.conversations import ConversationManager
from niucai.control.tasks import ComputerRequired, Conflict, require
from niucai.domain.schemas import ActionSpec, Plan, Role
from niucai.storage.db import Action, Message, Task, emit


def inline_schema(schema):
    """TypeBox and the model see a self-contained schema; Python validates again."""
    definitions = schema.get("$defs", {})

    def expand(value):
        if isinstance(value, list):
            return [expand(v) for v in value]
        if not isinstance(value, dict):
            return value
        if "$ref" in value:
            return expand(definitions[value["$ref"].split("/")[-1]])
        result = {k: expand(v) for k, v in value.items() if k not in {"$defs", "discriminator"}}
        # TypeBox's plain JSON Schema validator uses anyOf for union types.
        if "oneOf" in result:
            result["anyOf"] = result.pop("oneOf")
        return result

    return expand(schema)


class PiDurableRuntime:
    def __init__(self, gateway):
        self.gateway = gateway

    async def run(self, worker, task):
        token, task_id = task.run_token, task.id
        inbox = ConversationManager(worker.db)

        def current():
            with worker.db.sessions() as session:
                value = require(session, Task, task_id)
                worker.actions.check_task(value, token)
                return value

        current()
        storage_root = worker.settings.pi_storage.resolve()
        workspace = worker.settings.workspace.resolve()
        if storage_root == workspace or workspace in storage_root.parents:
            raise ValueError("Pi session storage must be outside the agent workspace")
        scope = task.conversation_id if task.checkpoint.get("session_scope") == "conversation" else task_id
        storage = storage_root / sha256(scope.encode()).hexdigest()
        storage.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = (storage / "writer.lock").open("a+")
        process = None
        serve = monitor = drain = None
        handlers = set()
        failure = asyncio.get_running_loop().create_future()
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise Conflict("Pi session still has a writer; retry after its exit") from exc
            needs_journal = task.checkpoint.get("pi_submission_id") is not None
            if scope == task.conversation_id:
                with worker.db.sessions() as session:
                    needs_journal = needs_journal or bool(
                        session.scalar(
                            select(Task.id)
                            .where(
                                Task.conversation_id == task.conversation_id,
                                Task.id != task_id,
                                Task.checkpoint["session_scope"].as_string() == "conversation",
                                Task.checkpoint["pi_submission_id"].as_string().is_not(None),
                            )
                            .limit(1)
                        )
                    )
            if needs_journal:
                journal = storage / "main.jsonl"
                if not journal.is_file() or journal.stat().st_size == 0:
                    raise ValueError(
                        "Pi session storage missing; restore pi_sessions with the database before retry"
                    )
            package = worker.context.compile(task_id)
            if not task.plan and task.checkpoint.get("session_scope") != "conversation":
                worker.tasks.phase(task_id, token, "PLANNING")
                plan = await self.gateway.structured(Role.PLANNER, package, Plan, task_id)
                current()
                worker.tasks.update(task_id, token, plan=plan.model_dump(mode="json"))
                package = worker.context.compile(task_id)
            prompt = (
                package["policies"]
                + " Use execute_action for every external computer/browser/file/shell operation. "
                "When remote Pi tools are available, delegate bounded independent work packages with "
                "remote_pi_submit and aggregate verified remote_pi_result outputs. Remote jobs require "
                "human workspace-autonomy approval; do not claim success while they are queued or running. "
                "Read tool feedback and adjust your approach in this session. FAILED permits correction; "
                "UNKNOWN requires human inspection. Use update_plan to revise the plan, task to delegate, "
                "and wait_for_human for login or clarification. A final answer completes the work. "
                "Kernel context at this run's start (later tool feedback supersedes observations): "
                + json.dumps(package, ensure_ascii=False)
            )
            node = shutil.which(worker.settings.pi_node)
            entrypoint = worker.settings.pi_entrypoint.resolve()
            if not node or not entrypoint.is_file():
                raise ValueError("build services/pi-runtime with Node 24: npm ci && npm run build")
            profile = self.gateway.router.resolve(Role.EXECUTOR) if hasattr(self.gateway, "router") else None
            config = {
                "taskId": task_id,
                "remotePiEnabled": worker.settings.remote_pi_enabled,
                "storage": str(storage),
                "prompt": prompt,
                "actionSchema": inline_schema(TypeAdapter(ActionSpec).json_schema()),
                "planSchema": inline_schema(Plan.model_json_schema()),
                "contextWindow": profile.context_window if profile else 64000,
                "maxTokens": profile.max_tokens if profile else 4096,
                "retryCount": task.retry_count,
                "requestId": task.checkpoint.get("pi_request_id"),
                "submissionId": task.checkpoint.get("pi_submission_id"),
                "freshRound": task.checkpoint.get("session_scope") == "conversation"
                and not task.checkpoint.get("pi_submission_id"),
            }
            pending_messages = inbox.pending(task_id)
            initial = pending_messages[0] if pending_messages else None
            if initial and (not config["requestId"] or task.checkpoint.get("pi_continue_inbox")):
                config.update(
                    requestId=initial.id,
                    submissionId=None,
                    inputContent=initial.content,
                    messageId=initial.id,
                )
            elif initial and initial.id == config["requestId"]:
                config.update(inputContent=initial.content, messageId=initial.id)
            for message in inbox.inputs(task_id):
                if message.id == config["requestId"]:
                    config.update(inputContent=message.content, messageId=message.id)
            config["replayMessages"] = [
                {"messageId": message.id, "content": message.content}
                for message in inbox.inputs(task_id)
                if message.id != config["requestId"]
            ]
            sent_ids = [message["messageId"] for message in config["replayMessages"]]
            sent = {config["messageId"]} if config.get("messageId") else set()
            sent.update(sent_ids)
            ready = asyncio.Event()
            current()
            # Inheritance ensures a killed Python owner cannot release a live Node writer's lock.
            process = await asyncio.create_subprocess_exec(
                node,
                str(entrypoint),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=8 * 1024 * 1024,
                pass_fds=(lock.fileno(),),
                env={k: os.environ[k] for k in ("PATH", "LANG", "TZ") if k in os.environ},
            )
            process.stdin.write((json.dumps(config, ensure_ascii=False) + "\n").encode())
            await process.stdin.drain()

            model_turns = 0

            async def rpc(method, params):
                nonlocal model_turns
                value = current()
                if method.startswith("remote."):
                    from fastapi.encoders import jsonable_encoder

                    from niucai.control.remote import TERMINAL, RemoteManager

                    remote = RemoteManager(worker.db, worker.settings)
                    remote.enabled()
                    if method == "remote.nodes":
                        return jsonable_encoder(remote.nodes())
                    if method == "remote.submit":
                        from niucai.api.remote import JobCreate

                        request = JobCreate(
                            prompt=params["prompt"],
                            allowed_nodes=params.get("allowed_nodes", []),
                            idempotency_key=f"{task_id}:remote:{sha256(str(params['toolTaskId']).encode()).hexdigest()}",
                            parent_task_id=task_id,
                        )
                        return jsonable_encoder(
                            remote.submit(
                                request.prompt, request.idempotency_key, request.allowed_nodes, task_id, token
                            )
                        )
                    if method == "remote.result":
                        for _ in range(20):
                            current()
                            job = await asyncio.to_thread(remote.get, params["job_id"])
                            if job["parent_task_id"] != task_id:
                                raise PermissionError("job belongs to another parent task")
                            if job["status"] == "WAITING_APPROVAL":
                                return {
                                    "wait": {
                                        "reason": "remote Pi approval required",
                                        "remote_job_id": job["id"],
                                    }
                                }
                            if job["status"] in TERMINAL | {"WAITING_APPROVAL", "LOST"}:
                                return jsonable_encoder(job)
                            worker.tasks.phase(task_id, token, "WAITING_REMOTE")
                            await asyncio.sleep(1)
                        return jsonable_encoder(job)
                    raise ValueError("unknown remote bridge method")
                if method == "model.chat":
                    with worker.db.sessions() as session:
                        input_sequence = session.scalar(
                            select(Message.sequence)
                            .where(
                                Message.task_id == task_id,
                                Message.role == "user",
                                Message.id.in_(params.get("inputIds", [])),
                            )
                            .order_by(Message.sequence.desc())
                            .limit(1)
                        )
                        if input_sequence is None:
                            # Old Pi sessions lack input envelopes; only their original goal is assumed seen.
                            input_sequence = session.scalar(
                                select(Message.sequence)
                                .where(Message.task_id == task_id, Message.role == "user")
                                .order_by(Message.sequence)
                                .limit(1)
                            )
                    if model_turns >= worker.settings.max_model_turns:
                        return {"wait": {"reason": "model turn budget exhausted"}}
                    model_turns += 1
                    role = Role(params["role"])
                    if role not in {Role.EXECUTOR, Role.SUMMARIZER}:
                        raise ValueError("unsupported harness model role")
                    worker.tasks.phase(task_id, token, "MODEL_REQUEST")
                    data = await self.gateway.chat(
                        role,
                        params["messages"],
                        task_id,
                        **({"tools": params["tools"]} if params.get("tools") else {}),
                    )
                    current()
                    # Persist the originating input revision in each Pi tool intent.
                    # This stays correct when parent and child model calls overlap.
                    for choice in data.get("choices", []):
                        for call in choice.get("message", {}).get("tool_calls", []):
                            if call.get("function", {}).get("name") == "execute_action":
                                try:
                                    arguments = json.loads(call["function"]["arguments"])
                                    if input_sequence is not None:
                                        arguments["_kernelSequence"] = input_sequence
                                        call["function"]["arguments"] = json.dumps(arguments)
                                except (ValueError, TypeError):
                                    pass  # Preserve the provider's normal invalid-arguments failure path.
                    worker.tasks.phase(task_id, token, "PROCESSING")
                    return data
                if method == "task.plan":
                    plan = Plan.model_validate(params["plan"])
                    worker.tasks.update(task_id, token, plan=plan.model_dump(mode="json"))
                    return {"plan_saved": True}
                if method == "task.wait":
                    key = str(params["toolTaskId"])
                    if value.checkpoint.get("pi_human_resumed") == key:
                        return {"human_resumed": True}
                    worker.tasks.update(
                        task_id,
                        token,
                        checkpoint={
                            **value.checkpoint,
                            "pi_human_wait": key,
                            "reason": "agent requested human",
                            "detail": str(params["reason"])[:4000],
                        },
                    )
                    return {
                        "wait": {"reason": "agent requested human", "detail": str(params["reason"])[:4000]}
                    }
                if method != "action.execute":
                    raise ValueError("unknown Pi bridge method")
                key = f"{task_id}:pi:{sha256(str(params['toolTaskId']).encode()).hexdigest()}"
                input_sequence = params.get("inputSequence")
                with worker.db.sessions() as session:
                    existing = session.scalar(select(Action).where(Action.idempotency_key == key))
                    latest = session.scalar(
                        select(Message.sequence)
                        .where(Message.task_id == task_id, Message.role == "user")
                        .order_by(Message.sequence.desc())
                        .limit(1)
                    )
                if not existing and input_sequence is not None and latest != input_sequence:
                    return {
                        "status": "DENIED",
                        "error": "User changed instructions; reconsider after steering.",
                    }
                if not existing and value.current_step >= value.checkpoint.get(
                    "budget_limit", worker.settings.max_steps
                ):
                    return {"wait": {"reason": "step budget exhausted"}}
                try:
                    action = worker.actions.propose(task_id, token, params["spec"], key, input_sequence)
                except ComputerRequired as exc:
                    return {"wait": {"reason": "computer required", "detail": str(exc)}}
                except (PermissionError, ValueError) as exc:
                    return {"status": "FAILED", "error": str(exc)[:500]}
                action = await worker.actions.execute(action.id, token)
                if action.status in {"WAITING_APPROVAL", "UNKNOWN"}:
                    return {"wait": {"reason": action.status, "action_id": action.id}}
                value = current()
                counted = value.checkpoint.get("counted_actions", [])
                if action.id not in counted:
                    worker.tasks.update(
                        task_id,
                        token,
                        current_step=value.current_step + 1,
                        checkpoint={
                            **value.checkpoint,
                            "counted_actions": [*counted, action.id],
                            "last_action_id": action.id,
                            "last_result": action.result,
                            "last_action_status": action.status,
                        },
                    )
                return {"action_id": action.id, "status": action.status, "result": action.result}

            async def respond(frame):
                try:
                    try:
                        response = {"id": frame["id"], "result": await rpc(frame["method"], frame["params"])}
                    except (ValueError, PermissionError) as exc:
                        response = {"id": frame["id"], "error": str(exc)[:500]}
                    process.stdin.write((json.dumps(response, ensure_ascii=False) + "\n").encode())
                    await process.stdin.drain()
                except asyncio.CancelledError:
                    raise
                except BaseException as exc:
                    if not failure.done():
                        failure.set_exception(exc)

            async def service():
                while line := await process.stdout.readline():
                    frame = json.loads(line)
                    if frame["type"] == "rpc":
                        # Keep reading admission acknowledgements during a model request.
                        handler = asyncio.create_task(respond(frame))
                        handlers.add(handler)
                        handler.add_done_callback(handlers.discard)
                    elif frame["type"] == "message_ack":
                        await asyncio.to_thread(
                            inbox.acknowledge, task_id, token, frame["messageId"], frame["submissionId"]
                        )
                    elif frame["type"] == "session":
                        ready.set()
                        value = current()
                        worker.tasks.update(
                            task_id,
                            token,
                            checkpoint={
                                **value.checkpoint,
                                "runtime": "pi",
                                "pi_conversation_id": frame["conversationId"],
                                "pi_submission_id": frame["submissionId"],
                                "pi_request_id": frame["requestId"],
                                "pi_continue_inbox": False,
                            },
                        )
                        with worker.db.sessions.begin() as session:
                            emit(
                                session,
                                "agent.started",
                                task_id,
                                runtime="pi",
                                conversation_id=frame["conversationId"],
                            )
                    elif frame["type"] == "outcome":
                        current()
                        if frame["status"] not in {"COMPLETED", "FAILED", "WAITING_HUMAN"}:
                            raise ValueError("invalid Pi outcome")
                        return {"status": frame["status"], "checkpoint": frame["checkpoint"]}
                    elif frame["type"] == "fatal":
                        raise RuntimeError(f"Pi runtime: {frame['error'][:500]}")
                raise RuntimeError(
                    "Pi process exited before reporting an outcome; retry preserves its session"
                )

            async def watch_lease():
                while True:
                    await asyncio.sleep(0.1)
                    current()
                    if not ready.is_set():
                        continue
                    for message in inbox.pending(task_id):
                        if message.id not in sent:
                            process.stdin.write(
                                (
                                    json.dumps(
                                        {
                                            "type": "input",
                                            "messageId": message.id,
                                            "content": message.content,
                                        },
                                        ensure_ascii=False,
                                    )
                                    + "\n"
                                ).encode()
                            )
                            await process.stdin.drain()
                            sent.add(message.id)

            async def drain_stderr():
                # Drain without logging prompts, keys or tool output.
                while await process.stderr.read(65536):
                    pass

            serve = asyncio.create_task(service())
            monitor = asyncio.create_task(watch_lease())
            drain = asyncio.create_task(drain_stderr())
            done, _ = await asyncio.wait({serve, monitor, failure}, return_when=asyncio.FIRST_COMPLETED)
            for future in done:
                if future is monitor or future is failure:
                    future.result()
            return serve.result()
        finally:

            async def cleanup():
                try:
                    for future in (serve, monitor, drain, failure, *handlers):
                        if future:
                            future.cancel()
                    if process:
                        if process.returncode is None:
                            try:
                                process.kill()
                            except ProcessLookupError:
                                pass
                        await process.wait()  # Keep storage locked until no child can write.
                    await asyncio.gather(
                        *(f for f in (serve, monitor, drain, failure, *handlers) if f), return_exceptions=True
                    )
                finally:
                    lock.close()

            # The inner lease watcher and Worker watchdog can revoke the same run.
            # A second cancellation must not interrupt child termination or unlock.
            cleaning = asyncio.create_task(cleanup())
            cancelled = False
            while not cleaning.done():
                try:
                    await asyncio.shield(cleaning)
                except asyncio.CancelledError:
                    cancelled = True
            cleaning.result()
            if cancelled:
                raise asyncio.CancelledError
