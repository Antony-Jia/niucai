"""白板：Bot、卡片状态机、领取与白板工具。

一张卡的每次执行是一条 remote_jobs（Attempt）；节点运行数 ≤ slots，Bot 运行数 ≤ concurrency。
Attempt 的引擎事件仍走 remote.job.* 事件流，card_events 只记卡片层面的时间线。
"""

import re
import secrets
from datetime import timedelta
from hashlib import sha256

from sqlalchemy import func, select

from niucai.control.remote import ACTIVE, ENGINES, NO_MCP_ENGINES, PROFILES, TERMINAL, engine_ready, job_event
from niucai.control.tasks import Conflict, require
from niucai.storage.db import (
    Bot,
    Card,
    CardEvent,
    Conversation,
    Event,
    McpServer,
    RemoteJob,
    RemoteNode,
    Skill,
    as_dict,
    emit,
    now,
    uid,
)

STATUSES = (
    "DRAFT",
    "READY",
    "WAITING_APPROVAL",
    "RUNNING",
    "REVIEW",
    "BLOCKED",
    "DONE",
    "FAILED",
    "CANCELLED",
)
CLOSED = {"DONE", "FAILED", "CANCELLED"}
EDITABLE = {"DRAFT", "READY", "WAITING_APPROVAL", "BLOCKED", "FAILED"}
BOT_NAME = re.compile(r"^[A-Za-z0-9_\-一-鿿]{1,60}$")
MENTION = re.compile(r"@([A-Za-z0-9_\-一-鿿]{1,60})")
BOT_CONFIG = (
    "engine",
    "role",
    "instructions",
    "tags",
    "model",
    "reasoning_effort",
    "permission_profile",
    "skills",
    "mcp_servers",
)
BOARD_TOOLS = (
    "board_view",
    "card_get",
    "inbox",
    "card_progress",
    "card_comment",
    "card_propose",
    "card_block",
    "card_submit",
)
# 防失控的硬上限：每个 Attempt 的进度条数、每张卡每小时评论数、每张卡的提议数。
PROGRESS_LIMIT, COMMENT_LIMIT, PROPOSAL_LIMIT = 200, 30, 10
INPUT_BUDGET = 200_000  # 依赖卡片产物随领取下发的总字节上限（内联传输）


def max_attempts(card):
    return int(card.budget.get("max_attempts", 3))


def max_seconds(card):
    minutes = card.budget.get("max_minutes")
    return int(minutes) * 60 if minutes else None


def over_time(s, job):
    """卡片 Attempt 超过 budget.max_minutes（留 60 秒余量给 Runner 自己结束）时，心跳让 Runner 停止。"""
    if not job.card_id:
        return False
    card = s.get(Card, job.card_id)
    limit = max_seconds(card) if card else None
    return bool(limit) and job.created_at + timedelta(seconds=limit + 60) < now()


def is_windows_node(node):
    return bool(node) and (node.resource_type == "windows_local" or node.platform == "windows")


def card_event(s, card, kind, actor, body="", data=None, mentions=(), key=None):
    """调用方须已锁定卡片行；seq 在卡片锁下分配。重复的幂等键返回已有事件。"""
    if key:
        existing = s.scalar(select(CardEvent).where(CardEvent.idempotency_key == key))
        if existing:
            if existing.card_id != card.id:
                raise Conflict("idempotency key reused on another card")
            return existing
    seq = (s.scalar(select(func.max(CardEvent.seq)).where(CardEvent.card_id == card.id)) or 0) + 1
    event = CardEvent(
        card_id=card.id,
        seq=seq,
        kind=kind,
        actor=actor,
        mentions=list(mentions),
        body=body[:16000],
        data=data or {},
        idempotency_key=key,
    )
    s.add(event)
    s.flush()
    # 全局事件流只带摘要，Desktop 收到后重读白板。
    emit(s, f"card.{kind}", card_id=card.id, seq=seq, status=card.status, actor=actor)
    from niucai.control.orchestrator import on_card_event

    on_card_event(s, card, event)
    return event


def set_status(s, card, status, actor, body="", **data):
    before = card.status
    card.status = status
    card.version += 1
    if status in CLOSED:
        remember_card(s, card)
    card_event(s, card, "status", actor, body, {"from": before, "to": status, **data})


def remember_card(s, card):
    """卡片结束时自动写一条卡片记忆：目标、结局与结果摘要，供编排 memory_search 与 Memory 页。"""
    from niucai.control.orchestrator import remember

    outcome = card.result.get("summary") or card.result.get("error") or ""
    content = (
        f"卡片：{card.title}\n目标：{card.goal[:2000]}\n结局：{card.status}\n结果：{str(outcome)[:6000]}"
    )
    tags = [card.status.lower(), *card.required_tags]
    remember(s, "card", card.id, content, card.assignee_bot_id, tags)


def mentions_of(s, body):
    names = set(MENTION.findall(body))
    known = {"user", "kernel"} & names
    bots = set(s.scalars(select(Bot.name).where(Bot.name.in_(names - known)))) if names - known else set()
    return sorted(known | bots)


def entry_status(s, card):
    """进入可领取状态时的落点：指定了需审批的 Bot 则先等人工批准。"""
    bot = s.get(Bot, card.assignee_bot_id) if card.assignee_bot_id else None
    return "WAITING_APPROVAL" if bot and bot.requires_approval else "READY"


def check_dependencies(s, card_id, depends_on):
    for ident in depends_on:
        require(s, Card, ident)
    # 依赖图不得成环：从新依赖出发不能回到本卡。
    seen, stack = set(), list(depends_on)
    while stack:
        ident = stack.pop()
        if ident == card_id:
            raise ValueError("card dependencies form a cycle")
        if ident in seen:
            continue
        seen.add(ident)
        row = s.get(Card, ident)
        stack.extend(row.depends_on if row else [])


def requeue(s, card, actor, reason):
    """失败或租约丢失后：未超预算回到 READY（下次领取续接会话），否则 FAILED。"""
    card.current_job_id = None
    if card.attempts < max_attempts(card):
        set_status(s, card, "READY", actor, reason, retry=True)
    else:
        card.result = {**card.result, "error": reason}
        set_status(s, card, "FAILED", actor, reason)


def job_finished(s, job):
    """RemoteManager.finish 的卡片钩子：把 Attempt 的结局映射到卡片状态。"""
    card = s.scalar(select(Card).where(Card.id == job.card_id).with_for_update())
    if not card or card.current_job_id != job.id or card.status != "RUNNING":
        return  # 卡片已被取消、重新排队或由其它 Attempt 接管。
    declared = card.result.get("job_id") == job.id
    outcome = card.result.get("outcome") if declared else None
    files = (job.result or {}).get("files", [])
    actor = f"bot:{job.bot_id}"
    if outcome == "blocked":
        card.current_job_id = None
        card.result = {**card.result, "resume": True}
        set_status(s, card, "BLOCKED", actor, card.result.get("question", ""))
    elif outcome == "submitted" or job.status == "COMPLETED":
        if outcome != "submitted":
            # 引擎未调用 card_submit（例如 Pi）：最终回答即提交摘要。
            card.result = {
                "job_id": job.id,
                "outcome": "submitted",
                "summary": str(job.result.get("answer", ""))[:16000],
            }
        card.result = {**card.result, "files": files, "resume": False}
        card.current_job_id = None
        set_status(s, card, "REVIEW" if card.review == "llm" else "DONE", actor, card.result["summary"][:500])
    else:
        error = str((job.result or {}).get("error", job.status))[:500]
        requeue(s, card, actor, f"Attempt {job.status}: {error}")


def card_inputs(s, card):
    """依赖卡片的结果摘要与内联文件，随领取下发，Runner 写入 inputs/<card_id>/。"""
    inputs, budget = [], INPUT_BUDGET
    for ident in card.depends_on:
        dependency = s.get(Card, ident)
        if not dependency:
            continue
        files = []
        for file in dependency.result.get("files", []):
            if "base64" not in file:
                continue
            size = len(file["base64"])
            if size > budget:
                break
            budget -= size
            files.append({"path": file["path"], "base64": file["base64"]})
        inputs.append(
            {
                "card_id": dependency.id,
                "title": dependency.title,
                "summary": str(dependency.result.get("summary", ""))[:4000],
                "files": files,
            }
        )
    return inputs


def compose_card_prompt(s, card, bot, inputs, since, board_tools=True):
    parts = [f"# 卡片：{card.title}", card.goal or "(无补充说明)"]
    if card.acceptance:
        parts += ["## 验收标准", card.acceptance]
    if inputs:
        parts.append("## 依赖卡片的结果（文件已放在 inputs/<card_id>/ 下）")
        parts += [f"- {i['card_id']}《{i['title']}》：{i['summary'] or '(无摘要)'}" for i in inputs]
    # 上次 Attempt 之后的评论/回答（例如解除阻塞、验收不通过的意见）放在提示开头之后。
    notes = list(
        s.scalars(
            select(CardEvent)
            .where(
                CardEvent.card_id == card.id,
                CardEvent.seq > since,
                CardEvent.kind.in_(["comment", "answer"]),
            )
            .order_by(CardEvent.seq)
            .limit(20)
        )
    )
    if notes:
        parts.append("## 新的评论与回答")
        parts += [f"- [{n.actor}] {n.body[:2000]}" for n in notes]
    if not board_tools:
        parts.append("完成后在最终回答中给出结果摘要；把要交付的文件写在工作区内。")
    else:
        parts.append(
            "用 niucai-board 工具汇报：card_progress 写进度，需要人或 kernel 回答时 card_block，"
            "完成后 card_submit 提交摘要并结束。把要交付的文件写在工作区内。"
        )
    return "\n\n".join(parts)


def node_occupancy(s, node_id):
    return s.scalar(
        select(func.count(RemoteJob.id)).where(RemoteJob.node_id == node_id, RemoteJob.status.in_(ACTIVE))
    )


def claim_card(s, settings, node):
    """在 RemoteManager.claim 的事务与调度锁内调用；返回新建的 Attempt 或 None。"""
    bots = [
        b
        for b in s.scalars(select(Bot).where(Bot.node_id == node.id, Bot.enabled == 1).order_by(Bot.name))
        if engine_ready(node.capabilities, b.engine)
    ]
    if not bots:
        return None
    running = dict(
        s.execute(
            select(RemoteJob.bot_id, func.count(RemoteJob.id))
            .where(RemoteJob.bot_id.in_([b.id for b in bots]), RemoteJob.status.in_(ACTIVE))
            .group_by(RemoteJob.bot_id)
        ).all()
    )
    free = [b for b in bots if running.get(b.id, 0) < b.concurrency]
    if not free:
        return None
    cards = s.scalars(
        select(Card)
        .where(Card.status == "READY")
        .order_by(Card.priority.desc(), Card.created_at)
        .with_for_update(skip_locked=True)
        .limit(200)
    )
    for card in cards:
        if not all(getattr(s.get(Card, ident), "status", None) == "DONE" for ident in card.depends_on):
            continue
        if card.assignee_bot_id:
            bot = next((b for b in free if b.id == card.assignee_bot_id), None)
        elif card.workdir:
            continue  # 本机目录只在指定节点上有意义：必须指派 Bot，不进公共池
        else:
            pool = [
                b for b in free if not b.requires_approval and set(card.required_tags) <= set(b.tags or [])
            ]
            bot = min(pool, key=lambda b: running.get(b.id, 0), default=None)
        if bot is None:
            continue
        if card.attempts >= max_attempts(card):
            # 预算在领取入口统一检查：验收退回、阻塞续接与失败重试都计入执行次数。
            reason = f"已达最大执行次数（{max_attempts(card)}）"
            card.result = {**card.result, "error": reason}
            set_status(s, card, "FAILED", "kernel", reason)
            continue
        return start_attempt(s, settings, card, bot, node)
    return None


def env_refs(config):
    """MCP 定义里 `env:NAME` 形式的值引用节点本地环境变量，实际值只在节点上。"""
    return sorted(
        {
            value[4:]
            for field in ("env", "headers")
            for value in (config.get(field) or {}).values()
            if isinstance(value, str) and value.startswith("env:")
        }
    )


def bot_assets(s, bot):
    """展开 Bot 绑定的 skill 与 MCP 定义（只取已启用的）；找不到的名称记入 missing。"""
    skills = (
        list(
            s.scalars(
                select(Skill).where(Skill.name.in_(bot.skills), Skill.enabled == 1).order_by(Skill.name)
            )
        )
        if bot.skills
        else []
    )
    servers = (
        list(
            s.scalars(
                select(McpServer)
                .where(McpServer.name.in_(bot.mcp_servers), McpServer.enabled == 1)
                .order_by(McpServer.name)
            )
        )
        if bot.mcp_servers
        else []
    )
    return {
        "skills": [
            {"name": x.name, "description": x.description, "content": x.content, "version": x.version}
            for x in skills
        ],
        "mcp_servers": [
            {
                "name": x.name,
                "transport": "stdio" if x.transport == "stdio" else "http",
                "config": {
                    k: v for k, v in x.config.items() if k in {"command", "args", "env", "url", "headers"}
                },
            }
            for x in servers
        ],
        "missing": sorted(
            {f"skill:{x}" for x in set(bot.skills) - {x.name for x in skills}}
            | {f"mcp:{x}" for x in set(bot.mcp_servers) - {x.name for x in servers}}
        ),
        "node_env": sorted({name for x in servers for name in env_refs(x.config)}),
    }


def start_attempt(s, settings, card, bot, node):
    previous = s.scalar(
        select(RemoteJob).where(RemoteJob.card_id == card.id).order_by(RemoteJob.created_at.desc()).limit(1)
    )
    since = 0
    if previous:
        marker = s.scalar(
            select(CardEvent.seq)
            .where(CardEvent.card_id == card.id, CardEvent.kind == "status")
            .where(CardEvent.data["attempt_job_id"].as_string() == previous.id)
            .limit(1)
        )
        since = marker or 0
    inputs = card_inputs(s, card)
    snapshot = {
        "permission_profile": bot.permission_profile,
        "instructions": bot.instructions,
        "bot": {
            "id": bot.id,
            "name": bot.name,
            "role": bot.role,
            "model": bot.model,
            "reasoning_effort": bot.reasoning_effort,
        },
        "config_version": bot.config_version,
        "card": {"id": card.id, "title": card.title},
        # 同一张卡的所有 Attempt 共用一个工作区，续接会话与产物都在其中。
        "workspace": f"card-{card.id}",
        "card_inputs": inputs,
        "board": True,
    }
    if card.workdir:
        snapshot["workdir"] = card.workdir
    if max_seconds(card):
        snapshot["timeout_seconds"] = max_seconds(card)
    assets = bot_assets(s, bot)
    # P5：skill 正文与 MCP 定义随 Attempt 下发，Runner 按引擎落地；解绑后下一次 Attempt 即不再带上。
    if assets["skills"]:
        snapshot["skills"] = assets["skills"]
    if assets["mcp_servers"] and bot.engine not in NO_MCP_ENGINES:
        snapshot["mcp_servers"] = assets["mcp_servers"]
    if (
        card.result.get("resume")
        and previous
        and previous.bot_id == bot.id
        and bot.engine != "pi"
        and previous.session_id
        and previous.session_id != previous.id
        and previous.status in TERMINAL
    ):
        snapshot["resume"] = {"job_id": previous.id, "session_id": previous.session_id}
    token = secrets.token_urlsafe(32)
    job = RemoteJob(
        # Pi 的白板工具由 Runner 扩展提供（features 含 pi-board）；旧 Runner 上仍以最终回答交付。
        prompt=compose_card_prompt(
            s,
            card,
            bot,
            inputs,
            since,
            # Hermes 在别处运行，用不了本节点的白板工具，和旧 Runner 上的 Pi 一样以最终回答交付。
            bot.engine not in NO_MCP_ENGINES
            or (bot.engine == "pi" and "pi-board" in (node.capabilities or {}).get("features", [])),
        ),
        engine=bot.engine,
        idempotency_key=f"card:{card.id}:{card.attempts + 1}:{uid()}",
        allowed_nodes=[node.id],
        config_snapshot=snapshot,
        card_id=card.id,
        bot_id=bot.id,
        board_token_hash=sha256(token.encode()).hexdigest(),
        node_id=node.id,
        attempt_id=uid(),
        status="RUNNING",
        event_sequence=0,
        lease_until=now() + timedelta(seconds=settings.remote_lease_seconds),
    )
    s.add(job)
    s.flush()
    card.attempts += 1
    card.current_job_id = job.id
    card.result = {k: v for k, v in card.result.items() if k in {"files", "summary"}}
    set_status(
        s, card, "RUNNING", "kernel", f"{bot.name} 开始第 {card.attempts} 次执行", attempt_job_id=job.id
    )
    job_event(s, job, "started", attempt_id=job.attempt_id, card_id=card.id, bot_id=bot.id)
    return {**as_dict(job), "artifact_transport": False, "board_token": token}


def revive(s, job, lease_seconds):
    """断网后同一 Attempt 回来：卡片仍指向它时把 LOST 恢复为 RUNNING，事件续传。"""
    if job.status != "LOST" or not job.card_id:
        return False
    card = s.get(Card, job.card_id)
    node = s.get(RemoteNode, job.node_id)
    if not card or card.current_job_id != job.id or card.status != "RUNNING" or not node or not node.enabled:
        return False
    # 同时续租：否则补交的下一条事件又会被 expire 判为 LOST，反复丢失/恢复。
    job.status, job.lease_until = "RUNNING", now() + timedelta(seconds=lease_seconds)
    job_event(s, job, "recovered")
    return True


def expire_cards(s, settings):
    """LOST 超过宽限期仍未回来的 Attempt：卡片重新排队（LOST 仍占用槽位直到进程确认退出）。"""
    deadline = now() - timedelta(seconds=settings.card_lost_grace_seconds)
    for job in s.scalars(
        select(RemoteJob).where(
            RemoteJob.status == "LOST", RemoteJob.card_id.is_not(None), RemoteJob.lease_until < deadline
        )
    ):
        card = s.scalar(select(Card).where(Card.id == job.card_id).with_for_update())
        if card and card.current_job_id == job.id and card.status == "RUNNING":
            requeue(s, card, "kernel", "Attempt 租约丢失超过宽限期")


class CardManager:
    def __init__(self, db, settings):
        self.db, self.settings = db, settings

    # ---- Bots ---------------------------------------------------------------

    @staticmethod
    def bot_view(bot, node=None, running=0, assets=None):
        value = as_dict(bot)
        value["requires_approval"] = bool(bot.requires_approval)
        value["enabled"] = bool(bot.enabled)
        value["running"] = running
        if assets is not None:
            # Desktop 提示：绑定了却不存在/已停用的名称，以及需在节点上设置的环境变量。
            value["missing_assets"] = assets["missing"]
            value["node_env"] = assets["node_env"]
        if node is not None:
            value["node_name"] = node.name
            value["node_heartbeat_at"] = node.heartbeat_at
        return value

    def bots(self):
        with self.db.sessions() as s:
            running = dict(
                s.execute(
                    select(RemoteJob.bot_id, func.count(RemoteJob.id))
                    .where(RemoteJob.bot_id.is_not(None), RemoteJob.status.in_(ACTIVE))
                    .group_by(RemoteJob.bot_id)
                ).all()
            )
            nodes = {n.id: n for n in s.scalars(select(RemoteNode))}
            return [
                self.bot_view(b, nodes.get(b.node_id), running.get(b.id, 0), bot_assets(s, b))
                for b in s.scalars(select(Bot).order_by(Bot.created_at))
            ]

    @staticmethod
    def validate_bot(s, values):
        if "name" in values and not BOT_NAME.fullmatch(values["name"]):
            raise ValueError("Bot 名称只能包含字母、数字、中文、- 与 _（用于 @名称）")
        if values.get("name") in {"user", "kernel"}:
            raise ValueError("user / kernel 是保留名称")
        if "engine" in values and values["engine"] not in ENGINES:
            raise ValueError(f"unknown engine: {values['engine']}")
        if "permission_profile" in values and values["permission_profile"] not in PROFILES:
            raise ValueError(f"unknown permission profile: {values['permission_profile']}")
        if values.get("engine") in NO_MCP_ENGINES and values.get("mcp_servers"):
            raise ValueError("Pi / Hermes Bot 暂不支持 MCP，请改用 Claude / Codex Bot")
        if "node_id" in values:
            require(s, RemoteNode, values["node_id"])

    def create_bot(self, values):
        with self.db.sessions.begin() as s:
            self.validate_bot(s, values)
            # §11：Windows 本机节点在用户自己的账户下运行，默认只读且每张卡需人工批准；显式放开仍保留。
            node = s.get(RemoteNode, values["node_id"])
            local = is_windows_node(node)
            values = {
                # Hermes 的权限由远端部署决定，节点只提供 workspace 档位。
                "permission_profile": "read" if local and values.get("engine") != "hermes" else "workspace",
                "requires_approval": local,
                **values,
            }
            if s.scalar(select(Bot.id).where(Bot.name == values["name"])):
                raise Conflict("Bot 名称已存在")
            bot = Bot(**{**values, "requires_approval": int(values.get("requires_approval", False))})
            s.add(bot)
            s.flush()
            emit(s, "bot.created", bot_id=bot.id, node_id=bot.node_id)
            return self.bot_view(bot)

    def update_bot(self, bot_id, changes):
        with self.db.sessions.begin() as s:
            bot = require(s, Bot, bot_id, lock=True)
            merged = {"engine": bot.engine, "mcp_servers": bot.mcp_servers, **changes}
            self.validate_bot(
                s, {**changes, "engine": merged["engine"], "mcp_servers": merged["mcp_servers"]}
            )
            if "name" in changes and s.scalar(
                select(Bot.id).where(Bot.name == changes["name"], Bot.id != bot_id)
            ):
                raise Conflict("Bot 名称已存在")
            if "node_id" in changes and changes["node_id"] != bot.node_id:
                if s.scalar(
                    select(RemoteJob.id).where(RemoteJob.bot_id == bot_id, RemoteJob.status.in_(ACTIVE))
                ):
                    raise Conflict("Bot 仍有执行中的卡片，不能迁移节点")
            for key, value in changes.items():
                setattr(bot, key, int(value) if key in {"requires_approval", "enabled"} else value)
            if any(key in changes for key in BOT_CONFIG):
                bot.config_version += 1  # Runner 据此重新落地配置
            emit(s, "bot.updated", bot_id=bot.id, node_id=bot.node_id)
            return self.bot_view(bot)

    def delete_bot(self, bot_id):
        with self.db.sessions.begin() as s:
            bot = require(s, Bot, bot_id, lock=True)
            if s.scalar(select(RemoteJob.id).where(RemoteJob.bot_id == bot_id).limit(1)) or s.scalar(
                select(Card.id).where(Card.assignee_bot_id == bot_id).limit(1)
            ):
                raise Conflict("Bot 已有执行记录或指派的卡片，请改为停用")
            s.delete(bot)
            emit(s, "bot.deleted", bot_id=bot_id)
            return {"deleted": bot_id}

    # ---- Cards --------------------------------------------------------------

    def card_view(self, s, card, bots=None):
        value = as_dict(card)
        bot = (bots or {}).get(card.assignee_bot_id) if bots is not None else None
        if bots is None and card.assignee_bot_id:
            bot = s.get(Bot, card.assignee_bot_id)
        value["assignee_name"] = bot.name if bot else None
        job = s.get(RemoteJob, card.current_job_id) if card.current_job_id else None
        value["current_job"] = (
            {"id": job.id, "status": job.status, "bot_id": job.bot_id, "engine": job.engine} if job else None
        )
        # BLOCKED / REVIEW 时 current_job_id 已清空，仍要能知道是哪个 Bot 做的：取最近一次 Attempt。
        last = job or s.scalar(
            select(RemoteJob)
            .where(RemoteJob.card_id == card.id)
            .order_by(RemoteJob.created_at.desc())
            .limit(1)
        )
        value["last_job"] = (
            {"id": last.id, "status": last.status, "bot_id": last.bot_id, "engine": last.engine}
            if last
            else None
        )
        # 卡片上显示最近一条动态：白板进度优先，否则取当前 Attempt 的最新引擎事件。
        latest = s.scalar(
            select(CardEvent)
            .where(CardEvent.card_id == card.id, CardEvent.kind.in_(["progress", "status", "question"]))
            .order_by(CardEvent.seq.desc())
            .limit(1)
        )
        value["latest"] = (
            {"kind": latest.kind, "body": latest.body[:300], "at": latest.created_at} if latest else None
        )
        # 协作地图要显示评论与 @提及：latest 保持原语义，这里另给最近几条带事件 ID 的动态。
        value["recent_events"] = [
            {
                "id": e.id,
                "kind": e.kind,
                "actor": e.actor,
                "body": e.body[:300],
                "mentions": list(e.mentions or []),
                "at": e.created_at,
            }
            for e in s.scalars(
                select(CardEvent)
                .where(
                    CardEvent.card_id == card.id,
                    CardEvent.kind.in_(["progress", "status", "question", "comment", "answer"]),
                )
                .order_by(CardEvent.seq.desc())
                .limit(5)
            )
        ]
        if job and job.status in ACTIVE:
            activity = s.scalar(
                select(Event)
                .where(Event.type == "remote.job.progress", Event.data["job_id"].as_string() == job.id)
                .order_by(Event.id.desc())
                .limit(1)
            )
            if activity and (not latest or activity.created_at >= latest.created_at):
                payload = activity.data.get("payload", {})
                text = payload.get("text") or payload.get("name") or activity.data.get("kind", "")
                value["latest"] = {"kind": "activity", "body": str(text)[:300], "at": activity.created_at}
        value.pop("result", None)
        value["summary"] = str(card.result.get("summary", ""))[:500]
        return value

    def board(self, conversation_id=None):
        with self.db.sessions() as s:
            bots = {b.id: b for b in s.scalars(select(Bot))}
            query = select(Card).order_by(Card.priority.desc(), Card.created_at).limit(500)
            if conversation_id:
                query = query.where(Card.conversation_id == conversation_id)
            else:
                # 全局白板不显示已删除任务留下的卡片。
                removed = select(Conversation.id).where(Conversation.deleted_at.is_not(None))
                query = query.where(Card.conversation_id.is_(None) | Card.conversation_id.not_in(removed))
            return {
                "cards": [self.card_view(s, c, bots) for c in s.scalars(query)],
                "bots": [
                    {"id": b.id, "name": b.name, "engine": b.engine, "enabled": bool(b.enabled)}
                    for b in bots.values()
                ],
                "statuses": list(STATUSES),
            }

    def get(self, card_id):
        with self.db.sessions() as s:
            card = require(s, Card, card_id)
            value = self.card_view(s, card)
            value["result"] = card.result
            value["events"] = [
                as_dict(e)
                for e in s.scalars(
                    select(CardEvent).where(CardEvent.card_id == card_id).order_by(CardEvent.seq)
                )
            ]
            value["runs"] = [
                {
                    "id": j.id,
                    "bot_id": j.bot_id,
                    "engine": j.engine,
                    "status": j.status,
                    "node_id": j.node_id,
                    "created_at": j.created_at,
                    "updated_at": j.updated_at,
                    "error": (j.result or {}).get("error"),
                }
                for j in s.scalars(
                    select(RemoteJob).where(RemoteJob.card_id == card_id).order_by(RemoteJob.created_at)
                )
            ]
            return value

    def create_card(self, values, actor="user"):
        with self.db.sessions.begin() as s:
            if values.get("assignee_bot_id"):
                require(s, Bot, values["assignee_bot_id"])
            if values.get("parent_card_id"):
                require(s, Card, values["parent_card_id"])
            draft = values.pop("draft", False)
            ident = uid()
            check_dependencies(s, ident, values.get("depends_on", []))
            card = Card(id=ident, created_by=actor, status="DRAFT", **values)
            s.add(card)
            s.flush()
            card_event(s, card, "status", actor, "创建卡片", {"from": None, "to": "DRAFT"})
            if not draft:
                set_status(s, card, entry_status(s, card), actor)
            return self.card_view(s, card)

    @staticmethod
    def locked(s, card_id, expected_version):
        card = s.scalar(select(Card).where(Card.id == card_id).with_for_update())
        if card is None:
            require(s, Card, card_id)
        if expected_version is not None and card.version != expected_version:
            raise Conflict("卡片已被修改，请刷新后重试")
        return card

    def update_card(self, card_id, expected_version, changes, actor="user"):
        with self.db.sessions.begin() as s:
            card = self.locked(s, card_id, expected_version)
            if card.status not in EDITABLE:
                raise Conflict(f"{card.status} 状态的卡片不能修改")
            if changes.get("assignee_bot_id"):
                require(s, Bot, changes["assignee_bot_id"])
            if "depends_on" in changes:
                check_dependencies(s, card.id, changes["depends_on"])
            for key, value in changes.items():
                setattr(card, key, value)
            card.version += 1
            card_event(s, card, "status", actor, "修改卡片", {"fields": sorted(changes), "to": card.status})
            if "assignee_bot_id" in changes and card.status in {"READY", "WAITING_APPROVAL"}:
                target = entry_status(s, card)
                if target != card.status:
                    set_status(s, card, target, actor)
            return self.card_view(s, card)

    def action(self, card_id, operation, expected_version=None, body="", actor="user"):
        with self.db.sessions.begin() as s:
            card = self.locked(s, card_id, expected_version)
            status = card.status
            if operation == "ready":
                if status != "DRAFT":
                    raise Conflict("只有草稿卡片可以发布")
                set_status(s, card, entry_status(s, card), actor, body)
            elif operation == "approve":
                if status != "WAITING_APPROVAL":
                    raise Conflict("卡片未在等待批准")
                set_status(s, card, "READY", actor, body or "已批准")
            elif operation == "cancel":
                if status in CLOSED:
                    raise Conflict("卡片已结束")
                if card.current_job_id:
                    job = s.get(RemoteJob, card.current_job_id)
                    if job and job.status not in TERMINAL:
                        job.status = "CANCELLING" if job.status in ACTIVE else "CANCELLED"
                        job_event(s, job, "cancel_requested", reason="card cancelled")
                card.current_job_id = None
                set_status(s, card, "CANCELLED", actor, body)
            elif operation in {"accept", "reject"}:
                if status != "REVIEW":
                    raise Conflict("卡片不在验收中")
                if operation == "reject":
                    if not body.strip():
                        raise ValueError("验收不通过需要附评论")
                    card_event(s, card, "comment", actor, body, mentions=mentions_of(s, body))
                    card.result = {**card.result, "resume": True}
                    set_status(s, card, entry_status(s, card), actor, "验收不通过")
                else:
                    set_status(s, card, "DONE", actor, body or "验收通过")
            elif operation == "answer":
                if status != "BLOCKED" or not body.strip():
                    raise Conflict("只有阻塞中的卡片可以回答，且回答不能为空")
                card_event(s, card, "answer", actor, body, mentions=mentions_of(s, body))
                set_status(s, card, entry_status(s, card), actor, "已回答")
            elif operation == "reopen":
                if status not in CLOSED:
                    raise Conflict("只有已结束的卡片可以重开")
                if body.strip():
                    card_event(s, card, "comment", actor, body, mentions=mentions_of(s, body))
                card.attempts = 0
                card.result = {**card.result, "resume": status == "DONE"}
                set_status(s, card, entry_status(s, card), actor, "重开")
            else:
                raise ValueError(f"unknown card operation: {operation}")
            return self.card_view(s, card)

    def comment(self, card_id, body, actor="user", key=None):
        with self.db.sessions.begin() as s:
            card = self.locked(s, card_id, None)
            event = card_event(s, card, "comment", actor, body, mentions=mentions_of(s, body), key=key)
            return as_dict(event)

    # ---- Board tools (engine side, scoped by the per-attempt board token) ---

    def board_call(self, node_id, job_id, token, tool, args):
        if tool not in BOARD_TOOLS:
            raise ValueError(f"unknown board tool: {tool}")
        with self.db.sessions.begin() as s:
            job = require(s, RemoteJob, job_id)
            if (
                job.node_id != node_id
                or not job.board_token_hash
                or not secrets.compare_digest(job.board_token_hash, sha256(token.encode()).hexdigest())
            ):
                raise PermissionError("invalid board token")
            revive(s, job, self.settings.remote_lease_seconds)
            card = s.scalar(select(Card).where(Card.id == job.card_id).with_for_update())
            live = job.status == "RUNNING" and card.current_job_id == job.id and card.status == "RUNNING"
            if not live and tool not in {"board_view", "card_get"}:
                raise Conflict("Attempt 已结束，白板只读")
            bot = require(s, Bot, job.bot_id)
            actor = f"bot:{bot.id}"
            return getattr(self, f"tool_{tool}")(s, job, card, bot, actor, args)

    def goal_card(self, s, card, ident):
        other = require(s, Card, ident)
        if other.conversation_id != card.conversation_id:
            raise PermissionError("card belongs to another goal")
        return other

    def tool_board_view(self, s, job, card, bot, actor, args):
        query = select(Card).order_by(Card.created_at).limit(100)
        query = query.where(
            Card.conversation_id == card.conversation_id
            if card.conversation_id
            else Card.conversation_id.is_(None)
        )
        return {
            "cards": [
                {
                    "id": c.id,
                    "title": c.title,
                    "status": c.status,
                    "assignee_bot_id": c.assignee_bot_id,
                    "summary": str(c.result.get("summary", ""))[:500],
                }
                for c in s.scalars(query)
            ]
        }

    def tool_card_get(self, s, job, card, bot, actor, args):
        other = self.goal_card(s, card, str(args.get("card_id") or card.id))
        events = list(
            s.scalars(
                select(CardEvent)
                .where(CardEvent.card_id == other.id)
                .order_by(CardEvent.seq.desc())
                .limit(20)
            )
        )
        return {
            "id": other.id,
            "title": other.title,
            "goal": other.goal,
            "acceptance": other.acceptance,
            "status": other.status,
            "summary": str(other.result.get("summary", ""))[:4000],
            "events": [
                {"seq": e.seq, "kind": e.kind, "actor": e.actor, "body": e.body[:2000]}
                for e in reversed(events)
            ],
        }

    def tool_inbox(self, s, job, card, bot, actor, args):
        cursor = int(job.wait_state.get("inbox_cursor", 0))
        # 卡片令牌只授权当前 Goal（无 Goal 的卡片只看无 Goal 的卡片）：不能读到别的 Goal 里的评论。
        scope = (
            Card.conversation_id == card.conversation_id
            if card.conversation_id
            else Card.conversation_id.is_(None)
        )
        scanned = list(
            s.scalars(
                select(CardEvent)
                .join(Card, Card.id == CardEvent.card_id)
                .where(CardEvent.id > cursor, CardEvent.actor != actor, scope)
                .order_by(CardEvent.id)
                .limit(500)
            )
        )
        rows = [e for e in scanned if bot.name in (e.mentions or [])][:50]
        last = rows[-1].id if len(rows) == 50 else (scanned[-1].id if scanned else None)
        if last:
            job.wait_state = {**job.wait_state, "inbox_cursor": last}
        return {
            "messages": [
                {"card_id": e.card_id, "actor": e.actor, "kind": e.kind, "body": e.body[:2000]} for e in rows
            ]
        }

    def tool_card_progress(self, s, job, card, bot, actor, args):
        text = str(args.get("text", "")).strip()
        if not text:
            raise ValueError("progress text required")
        count = s.scalar(
            select(func.count(CardEvent.id)).where(
                CardEvent.card_id == card.id,
                CardEvent.kind == "progress",
                CardEvent.data["job_id"].as_string() == job.id,
            )
        )
        if count >= PROGRESS_LIMIT:
            raise Conflict("progress limit reached for this attempt")
        data = {"job_id": job.id}
        if isinstance(args.get("percent"), int | float):
            data["percent"] = max(0, min(100, int(args["percent"])))
        card_event(s, card, "progress", actor, text[:2000], data, key=self.tool_key(job, args))
        return {"ok": True}

    def tool_card_comment(self, s, job, card, bot, actor, args):
        target = self.goal_card(s, card, str(args.get("card_id") or card.id))
        if target.id != card.id:
            target = s.scalar(select(Card).where(Card.id == target.id).with_for_update())
        body = str(args.get("body", "")).strip()
        if not body:
            raise ValueError("comment body required")
        recent = s.scalar(
            select(func.count(CardEvent.id)).where(
                CardEvent.card_id == target.id,
                CardEvent.actor == actor,
                CardEvent.kind == "comment",
                CardEvent.created_at > now() - timedelta(hours=1),
            )
        )
        if recent >= COMMENT_LIMIT:
            raise Conflict("comment rate limit reached")
        event = card_event(
            s,
            target,
            "comment",
            actor,
            body,
            {"job_id": job.id},
            mentions_of(s, body),
            self.tool_key(job, args),
        )
        return {"seq": event.seq}

    def tool_card_propose(self, s, job, card, bot, actor, args):
        title = str(args.get("title", "")).strip()[:200]
        if not title:
            raise ValueError("proposal title required")
        count = s.scalar(
            select(func.count(CardEvent.id)).where(CardEvent.card_id == card.id, CardEvent.kind == "proposal")
        )
        if count >= PROPOSAL_LIMIT:
            raise Conflict("proposal limit reached for this card")
        proposal = {
            "title": title,
            "goal": str(args.get("goal", ""))[:8000],
            "acceptance": str(args.get("acceptance", ""))[:4000],
            "bot": str(args.get("bot", ""))[:60],
            "tags": [str(t)[:40] for t in args.get("tags", [])][:10]
            if isinstance(args.get("tags"), list)
            else [],
        }
        # 提议只记事件，由用户或编排 LLM 决定是否转成卡片；Bot 不能直接建 READY 卡。
        card_event(
            s,
            card,
            "proposal",
            actor,
            title,
            {"job_id": job.id, "proposal": proposal},
            ["kernel"],
            self.tool_key(job, args),
        )
        return {"ok": True}

    def tool_card_block(self, s, job, card, bot, actor, args):
        question = str(args.get("question", "")).strip()
        if not question:
            raise ValueError("question required")
        mention = args.get("mention") if args.get("mention") in {"user", "kernel"} else "user"
        card.result = {**card.result, "job_id": job.id, "outcome": "blocked", "question": question[:4000]}
        card.version += 1
        card_event(
            s, card, "question", actor, question, {"job_id": job.id}, [mention], self.tool_key(job, args)
        )
        return {"ok": True, "next": "结束本次执行，等待回答后会续接会话"}

    def tool_card_submit(self, s, job, card, bot, actor, args):
        summary = str(args.get("summary", "")).strip()
        if not summary:
            raise ValueError("summary required")
        card.result = {
            **card.result,
            "job_id": job.id,
            "outcome": "submitted",
            "summary": summary[:16000],
        }
        card.version += 1
        card_event(
            s,
            card,
            "progress",
            actor,
            "已提交结果",
            {"job_id": job.id, "submitted": True},
            key=self.tool_key(job, args),
        )
        return {"ok": True, "next": "结束本次执行"}

    @staticmethod
    def tool_key(job, args):
        key = args.get("idempotency_key")
        return f"{job.id}:{str(key)[:120]}" if key else None
