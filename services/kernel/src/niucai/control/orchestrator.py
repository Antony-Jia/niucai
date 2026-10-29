"""编排 LLM（重构 P3）：按 Goal 事件驱动的 planner 工具循环。

编排只能通过白板工具指挥 Bot：拆卡、指派、验收、回答阻塞、决定提议、回复用户。
没有浏览器、Shell 或写文件工具；真实执行全部发生在节点上的 Bot 里。
每个 Goal 同时最多一个 RUNNING 运行（会话行锁 + 租约），新触发合并进 PENDING 运行。
"""

import asyncio
import base64
import json
import logging
from datetime import timedelta

from sqlalchemy import func, or_, select

from niucai.control.cards import CLOSED, CardManager
from niucai.control.conversations import append_message, lock_conversation
from niucai.control.tasks import Conflict, Missing, require
from niucai.domain.schemas import Role
from niucai.storage.db import (
    Bot,
    Card,
    CardEvent,
    Conversation,
    Memory,
    Message,
    OrchestratorRun,
    as_dict,
    emit,
    now,
)

log = logging.getLogger("niucai.orchestrator")
ACTOR = "kernel"
TRIGGER_LIMIT = 20  # 一个运行最多合并的触发数，更早的丢弃（上下文里只看最近的）
CARD_LIMIT = 50  # 每个 Goal 的卡片上限，防止编排失控拆卡
MESSAGE_WINDOW = 20
RECOVER_LIMIT = 2  # 同一批触发因 Worker 崩溃被重新排队的次数上限，防止崩溃循环

SYSTEM_PROMPT = """你是 niucai Kernel 的编排者（@kernel）。
用户给出一个 Goal，你负责把它拆成白板卡片交给 Bot 执行。
规则：
- 你只能使用提供的白板工具；你不能自己浏览网页、执行命令或写文件，所有实际工作由 Bot 完成。
- 先用 bots_list 了解可用 Bot；按 Bot 的角色与标签指派。没有合适 Bot 时用 ask_user 告诉用户。
- 卡片要写清 goal（做什么）和 acceptance（怎样算完成）；有先后关系时用 depends_on 填前置卡片 id。
- 卡片进入 REVIEW 时对照验收标准用 card_review 给出 accept 或 reject（reject 必须写明原因）。
- Bot 阻塞并 @kernel 提问时，能回答就用 card_comment 回答，否则用 ask_user 转问用户。
- Bot 的提议用 proposal_decide 决定是否采纳。
- 需要对用户说话时用 goal_reply；全部卡片结束后用 goal_reply(final=true) 给出最终总结。
- 不要重复创建已存在的卡片；每次运行只处理触发事件需要的工作，处理完就结束（不再调用工具）。"""


def tool(name, description, properties=None, required=()):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties or {},
                "required": list(required),
                "additionalProperties": False,
            },
        },
    }


STRING, BOOL, INT = {"type": "string"}, {"type": "boolean"}, {"type": "integer"}
STRINGS = {"type": "array", "items": {"type": "string"}}
TOOLS = [
    tool("bots_list", "列出全部 Bot：名称、引擎、角色、标签、是否可用、所在节点状态与运行数"),
    tool("card_get", "读取一张卡片的详情、最近时间线与结果摘要", {"card_id": STRING}, ["card_id"]),
    tool(
        "card_create",
        "在当前 Goal 下创建卡片；bot 为 Bot 名称（留空进公共池按标签领取）",
        {
            "title": STRING,
            "goal": STRING,
            "acceptance": STRING,
            "bot": STRING,
            "tags": STRINGS,
            "depends_on": STRINGS,
            "review": {"type": "string", "enum": ["none", "llm"]},
            "priority": INT,
        },
        ["title", "goal"],
    ),
    tool(
        "card_update",
        "修改未运行卡片的标题、目标、验收标准、指派、依赖或优先级；version 取自 card_get，卡片已变则拒绝",
        {
            "card_id": STRING,
            "version": INT,
            "title": STRING,
            "goal": STRING,
            "acceptance": STRING,
            "bot": STRING,
            "depends_on": STRINGS,
            "priority": INT,
        },
        ["card_id", "version"],
    ),
    tool(
        "card_review",
        "验收 REVIEW 状态的卡片；reject 必须附 comment，卡片会带意见退回同一 Bot",
        {"card_id": STRING, "verdict": {"type": "string", "enum": ["accept", "reject"]}, "comment": STRING},
        ["card_id", "verdict"],
    ),
    tool(
        "card_cancel", "取消卡片（运行中会停止当前执行）", {"card_id": STRING, "reason": STRING}, ["card_id"]
    ),
    tool(
        "proposal_decide",
        "决定 Bot 的提议：accept 会按提议建卡（可用 bot 覆盖指派），reject 只记录",
        {
            "card_id": STRING,
            "seq": INT,
            "decision": {"type": "string", "enum": ["accept", "reject"]},
            "bot": STRING,
            "after_source": {
                "type": "boolean",
                "description": "默认 true：新卡依赖原卡并带上其产物；可并行的子任务传 false",
            },
            "note": STRING,
        },
        ["card_id", "seq", "decision"],
    ),
    tool(
        "card_comment",
        "在卡片上评论；卡片 BLOCKED 时这条评论就是回答，卡片会回到队列续接",
        {"card_id": STRING, "body": STRING},
        ["card_id", "body"],
    ),
    tool("ask_user", "向用户提问（显示在 Goal 对话里）", {"question": STRING}, ["question"]),
    tool(
        "goal_reply",
        "在 Goal 对话里回复用户；final=true 表示 Goal 完成并写入 Goal 记忆",
        {"text": STRING, "final": BOOL},
        ["text"],
    ),
    tool(
        "memory_search",
        "按关键词搜索历史记忆（卡片与 Goal 的执行记录），返回最多 10 条",
        {"query": STRING, "kind": {"type": "string", "enum": ["card", "goal"]}},
        ["query"],
    ),
    tool(
        "artifact_read",
        "读取卡片结果里的文件；不给 path 时列出文件",
        {"card_id": STRING, "path": STRING},
        ["card_id"],
    ),
]


def is_goal(conversation):
    return bool(conversation) and (conversation.settings or {}).get("mode") == "goal"


def enqueue(s, conversation_id, trigger):
    """记录一次触发：合并进该 Goal 已有的 PENDING 运行，没有则新建。"""
    run = s.scalar(
        select(OrchestratorRun)
        .where(OrchestratorRun.conversation_id == conversation_id, OrchestratorRun.status == "PENDING")
        .order_by(OrchestratorRun.created_at)
        .limit(1)
        .with_for_update()
    )
    trigger = {**trigger, "at": now().isoformat()}
    if run:
        run.triggers = [*run.triggers, trigger][-TRIGGER_LIMIT:]
    else:
        run = OrchestratorRun(conversation_id=conversation_id, status="PENDING", triggers=[trigger])
        s.add(run)
    s.flush()
    emit(s, "orchestrator.queued", conversation_id=conversation_id, run_id=run.id, trigger=trigger["kind"])
    return run


def on_card_event(s, card, event):
    """card_event 的钩子：Goal 卡片上需要编排关注的事件转成触发。编排自己的动作不再触发。"""
    if not card.conversation_id or event.actor == ACTOR:
        return
    conversation = s.get(Conversation, card.conversation_id)
    if not is_goal(conversation):
        return
    kind = None
    if event.kind == "status":
        target = (event.data or {}).get("to")
        if target == "REVIEW" and card.review == "llm":
            kind = "review"
        elif target in {"DONE", "FAILED"}:
            kind = target.lower()
        elif target == "BLOCKED" and asked_kernel(s, card):
            kind = "blocked"
        elif target == "CANCELLED":
            kind = "cancelled"
        if kind and target in CLOSED and all_closed(s, card.conversation_id):
            kind = "all_closed"
    elif event.kind == "proposal":
        kind = "proposal"
    elif event.kind in {"comment", "answer"} and ACTOR in (event.mentions or []):
        kind = "mention"
    if kind is None or (kind == "cancelled" and event.actor == "user"):
        return  # 用户亲手取消的卡片不需要编排介入，除非这让 Goal 全部结束
    enqueue(
        s,
        card.conversation_id,
        {
            "kind": kind,
            "card_id": card.id,
            "title": card.title,
            "status": card.status,
            "seq": event.seq,
            "actor": event.actor,
            "body": (event.body or "")[:1000],
        },
    )


def asked_kernel(s, card):
    question = s.scalar(
        select(CardEvent)
        .where(CardEvent.card_id == card.id, CardEvent.kind == "question")
        .order_by(CardEvent.seq.desc())
        .limit(1)
    )
    return bool(question) and ACTOR in (question.mentions or [])


def all_closed(s, conversation_id):
    statuses = list(s.scalars(select(Card.status).where(Card.conversation_id == conversation_id)))
    return bool(statuses) and all(status in CLOSED for status in statuses)


class Orchestrator:
    def __init__(self, db, settings, gateway):
        self.db, self.settings, self.gateway = db, settings, gateway
        self.cards = CardManager(db, settings)

    # ---- Claim ---------------------------------------------------------------

    def claim(self):
        """领取最早的 PENDING 运行；同一 Goal 已有未过期 RUNNING 时跳过，并合并同 Goal 的其它 PENDING。"""
        with self.db.sessions.begin() as s:
            self.recover(s)
        with self.db.sessions.begin() as s:
            pending = s.execute(
                select(OrchestratorRun.id, OrchestratorRun.conversation_id)
                .where(OrchestratorRun.status == "PENDING")
                .order_by(OrchestratorRun.created_at)
                .limit(20)
            ).all()
            seen = set()
            for run_id, conversation_id in pending:
                if conversation_id in seen:
                    continue
                seen.add(conversation_id)
                lock_conversation(s, conversation_id)
                if self.busy(s, conversation_id) or self.throttled(s, conversation_id):
                    continue
                run = require(s, OrchestratorRun, run_id, lock=True)
                if run.status != "PENDING":
                    continue
                for other in s.scalars(
                    select(OrchestratorRun).where(
                        OrchestratorRun.conversation_id == conversation_id,
                        OrchestratorRun.status == "PENDING",
                        OrchestratorRun.id != run.id,
                    )
                ):
                    run.triggers = [*run.triggers, *other.triggers][-TRIGGER_LIMIT:]
                    s.delete(other)
                run.status, run.started_at = "RUNNING", now()
                run.lease_until = now() + timedelta(seconds=self.settings.orchestrator_lease_seconds)
                emit(s, "orchestrator.started", conversation_id=conversation_id, run_id=run.id)
                return {"id": run.id, "conversation_id": conversation_id, "triggers": list(run.triggers)}
        return None

    def recover(self, s):
        """独立扫描租约过期的 RUNNING 运行：判失败，并把原触发重新排队，不依赖同一 Goal 再有新事件。"""
        expired = list(
            s.scalars(
                select(OrchestratorRun)
                .where(OrchestratorRun.status == "RUNNING", OrchestratorRun.lease_until < now())
                .order_by(OrchestratorRun.lease_until)
                .limit(20)
                .with_for_update(skip_locked=True)
            )
        )
        for run in expired:
            self.expire(s, run)

    def expire(self, s, run):
        run.status, run.finished_at, run.error, run.lease_until = "FAILED", now(), "lease expired", None
        emit(s, "orchestrator.failed", conversation_id=run.conversation_id, run_id=run.id)
        triggers = [t for t in run.triggers or [] if t.get("kind") != "recovered"]
        attempts = sum(1 for t in run.triggers or [] if t.get("kind") == "recovered")
        if not triggers or attempts >= RECOVER_LIMIT:
            log.warning("orchestrator run %s expired and will not be retried", run.id)
            return
        pending = enqueue(s, run.conversation_id, {"kind": "recovered", "run_id": run.id})
        marks = [{"kind": "recovered", "run_id": run.id}] * attempts
        pending.triggers = [*marks, *triggers, *pending.triggers][-TRIGGER_LIMIT:]

    def busy(self, s, conversation_id):
        running = s.scalar(
            select(OrchestratorRun)
            .where(OrchestratorRun.conversation_id == conversation_id, OrchestratorRun.status == "RUNNING")
            .limit(1)
        )
        if running and running.lease_until and running.lease_until < now():
            # Worker 在运行中退出：租约过期的运行判失败并重新排队原触发，Goal 可以继续被编排。
            self.expire(s, running)
            return False
        return running is not None

    def throttled(self, s, conversation_id):
        started = s.scalar(
            select(func.count(OrchestratorRun.id)).where(
                OrchestratorRun.conversation_id == conversation_id,
                OrchestratorRun.started_at >= now() - timedelta(hours=1),
            )
        )
        return started >= self.settings.orchestrator_runs_per_hour

    # ---- Run -----------------------------------------------------------------

    async def run_once(self):
        claimed = await asyncio.to_thread(self.claim)
        if not claimed:
            return False
        await self.execute(claimed)
        return True

    async def execute(self, claimed):
        run_id, goal_id = claimed["id"], claimed["conversation_id"]
        steps, usage, output, calls = [], {}, "", 0
        try:
            messages = await asyncio.to_thread(self.context, goal_id, claimed["triggers"])
            for _ in range(self.settings.orchestrator_max_steps):
                data = await self.gateway.chat(Role.PLANNER, messages, None, tools=TOOLS, tool_choice="auto")
                for key, value in (data.get("usage") or {}).items():
                    if isinstance(value, int):
                        usage[key] = usage.get(key, 0) + value
                message = data["choices"][0]["message"]
                tool_calls = message.get("tool_calls") or []
                if not tool_calls:
                    output = message.get("content") or ""
                    break
                messages.append(
                    {"role": "assistant", "content": message.get("content") or "", "tool_calls": tool_calls}
                )
                for call in tool_calls:
                    calls += 1
                    name = call.get("function", {}).get("name", "")
                    if calls > self.settings.orchestrator_max_tool_calls:
                        result = {"error": "本次运行的工具调用次数已达上限，请结束"}
                    else:
                        result = await asyncio.to_thread(
                            self.call, run_id, goal_id, name, call.get("function", {}).get("arguments")
                        )
                    steps.append({"tool": name, "args": parse_args(call), "result": clip(result, 2000)})
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.get("id", ""),
                            "content": clip(result, self.settings.tool_result_chars),
                        }
                    )
                await asyncio.to_thread(self.checkpoint, run_id, steps, usage)
            else:
                output = "已达到单次运行的最大轮数"
            await asyncio.to_thread(self.close, run_id, "COMPLETED", steps, usage, output, None)
        except Exception as exc:
            log.exception("orchestrator run %s failed", run_id)
            await asyncio.to_thread(self.close, run_id, "FAILED", steps, usage, output, str(exc)[:2000])

    def checkpoint(self, run_id, steps, usage):
        with self.db.sessions.begin() as s:
            run = require(s, OrchestratorRun, run_id, lock=True)
            if run.status != "RUNNING":
                raise Conflict("orchestrator run lease was lost")  # 已被回收，停止本次运行
            run.steps, run.usage = list(steps), dict(usage)
            run.lease_until = now() + timedelta(seconds=self.settings.orchestrator_lease_seconds)

    def close(self, run_id, status, steps, usage, output, error):
        with self.db.sessions.begin() as s:
            run = require(s, OrchestratorRun, run_id, lock=True)
            if run.status != "RUNNING":
                return  # 租约已过期并被回收，过期的执行者不再写入
            run.status, run.steps, run.usage = status, list(steps), dict(usage)
            run.output, run.error, run.finished_at, run.lease_until = output[:16000], error, now(), None
            emit(
                s,
                f"orchestrator.{status.lower()}",
                conversation_id=run.conversation_id,
                run_id=run.id,
                tool_calls=len(steps),
            )

    def context(self, goal_id, triggers):
        """每次运行只给：Goal 对话最近消息、本 Goal 白板快照、可用 Bot、触发详情；不带长历史。"""
        with self.db.sessions() as s:
            conversation = require(s, Conversation, goal_id)
            recent = list(
                s.scalars(
                    select(Message)
                    .where(Message.conversation_id == goal_id)
                    .order_by(Message.sequence.desc())
                    .limit(MESSAGE_WINDOW)
                )
            )[::-1]
        board = self.cards.board(goal_id)
        package = {
            "goal": {"id": goal_id, "title": conversation.title, "status": goal_status(conversation)},
            "messages": [
                {
                    "from": "user" if m.role == "user" else "kernel",
                    "kind": m.kind,
                    "content": m.content[:4000],
                }
                for m in recent
            ],
            "board": [
                {
                    "id": c["id"],
                    "title": c["title"],
                    "status": c["status"],
                    "bot": c["assignee_name"],
                    "depends_on": c["depends_on"],
                    "review": c["review"],
                    "attempts": c["attempts"],
                    "summary": c["summary"],
                    "latest": (c["latest"] or {}).get("body", ""),
                }
                for c in board["cards"]
            ],
            "bots": self.bots_list(),
            "triggers": triggers,
        }
        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(package, ensure_ascii=False, default=str)},
        ]

    def call(self, run_id, goal_id, name, raw):
        try:
            args = json.loads(raw) if isinstance(raw, str) and raw.strip() else (raw or {})
            if not isinstance(args, dict):
                raise ValueError("tool arguments must be a JSON object")
            handler = getattr(self, f"tool_{name}", None) if name in TOOL_NAMES else None
            if handler is None:
                raise ValueError(f"unknown tool: {name}")
            return handler(goal_id, args)
        except (Conflict, Missing, ValueError, PermissionError, LookupError, TypeError) as exc:
            return {"error": str(exc)}

    # ---- Tools ---------------------------------------------------------------

    def goal_card(self, s, goal_id, card_id):
        card = require(s, Card, str(card_id))
        if card.conversation_id != goal_id:
            raise PermissionError("card belongs to another goal")
        return card

    def bot_id(self, s, name):
        if not name:
            return None
        bot = s.scalar(select(Bot).where(Bot.name == str(name).lstrip("@")))
        if not bot:
            raise ValueError(f"没有名为 {name} 的 Bot；先调用 bots_list")
        if not bot.enabled:
            raise ValueError(f"Bot {name} 已停用")
        return bot.id

    def bots_list(self):
        return [
            {
                "name": b["name"],
                "engine": b["engine"],
                "role": b["role"],
                "tags": b["tags"],
                "enabled": b["enabled"],
                "node": b.get("node_name"),
                "node_online": bool(b.get("node_heartbeat_at"))
                and b["node_heartbeat_at"] > now() - timedelta(seconds=self.settings.remote_lease_seconds),
                "running": b.get("running", 0),
                "concurrency": b["concurrency"],
            }
            for b in self.cards.bots()
        ]

    def tool_bots_list(self, goal_id, args):
        return {"bots": self.bots_list()}

    def tool_card_get(self, goal_id, args):
        with self.db.sessions() as s:
            self.goal_card(s, goal_id, args.get("card_id"))
        value = self.cards.get(str(args["card_id"]))
        return {
            "id": value["id"],
            "title": value["title"],
            "goal": value["goal"],
            "acceptance": value["acceptance"],
            "status": value["status"],
            "bot": value["assignee_name"],
            "version": value["version"],
            "summary": str(value["result"].get("summary", ""))[:4000],
            "question": value["result"].get("question"),
            "files": [f.get("path") for f in value["result"].get("files", [])][:50],
            "events": [
                {
                    "seq": e["seq"],
                    "kind": e["kind"],
                    "actor": e["actor"],
                    "body": e["body"][:500],
                    "data": e["data"],
                }
                for e in value["events"][-20:]
            ],
        }

    def tool_card_create(self, goal_id, args):
        with self.db.sessions() as s:
            count = s.scalar(select(func.count(Card.id)).where(Card.conversation_id == goal_id))
            if count >= CARD_LIMIT:
                raise Conflict(f"每个 Goal 最多 {CARD_LIMIT} 张卡片")
            bot_id = self.bot_id(s, args.get("bot"))
            depends = [str(d) for d in args.get("depends_on") or []][:32]
            for ident in depends:
                self.goal_card(s, goal_id, ident)
        card = self.cards.create_card(
            {
                "title": str(args.get("title", "")).strip()[:200] or "未命名卡片",
                "goal": str(args.get("goal", ""))[:16000],
                "acceptance": str(args.get("acceptance", ""))[:8000],
                "conversation_id": goal_id,
                "assignee_bot_id": bot_id,
                "required_tags": [str(t)[:40] for t in args.get("tags") or []][:20],
                "depends_on": depends,
                "review": "llm" if args.get("review") == "llm" else "none",
                "priority": max(-100, min(100, int(args.get("priority") or 0))),
                "budget": {"max_attempts": 3},
            },
            actor=ACTOR,
        )
        return {"card_id": card["id"], "status": card["status"]}

    def tool_card_update(self, goal_id, args):
        changes = {}
        with self.db.sessions() as s:
            self.goal_card(s, goal_id, args.get("card_id"))
            if "bot" in args:
                changes["assignee_bot_id"] = self.bot_id(s, args["bot"])
            if "depends_on" in args:
                changes["depends_on"] = [str(d) for d in args["depends_on"] or []][:32]
                for ident in changes["depends_on"]:
                    self.goal_card(s, goal_id, ident)
        for key, limit in (("title", 200), ("goal", 16000), ("acceptance", 8000)):
            if key in args:
                changes[key] = str(args[key])[:limit]
        if "priority" in args:
            changes["priority"] = max(-100, min(100, int(args["priority"] or 0)))
        if not changes:
            raise ValueError("nothing to update")
        if not isinstance(args.get("version"), int):
            raise ValueError("version is required; read it with card_get")
        card = self.cards.update_card(str(args["card_id"]), args["version"], changes, actor=ACTOR)
        return {"card_id": card["id"], "status": card["status"], "version": card["version"]}

    def tool_card_review(self, goal_id, args):
        with self.db.sessions() as s:
            self.goal_card(s, goal_id, args.get("card_id"))
        verdict = args.get("verdict")
        if verdict not in {"accept", "reject"}:
            raise ValueError("verdict must be accept or reject")
        card = self.cards.action(str(args["card_id"]), verdict, None, str(args.get("comment", "")), ACTOR)
        return {"card_id": card["id"], "status": card["status"]}

    def tool_card_cancel(self, goal_id, args):
        with self.db.sessions() as s:
            self.goal_card(s, goal_id, args.get("card_id"))
        card = self.cards.action(str(args["card_id"]), "cancel", None, str(args.get("reason", "")), ACTOR)
        return {"card_id": card["id"], "status": card["status"]}

    def tool_proposal_decide(self, goal_id, args):
        card_id, seq = str(args.get("card_id")), int(args.get("seq") or 0)
        decision = args.get("decision")
        if decision not in {"accept", "reject"}:
            raise ValueError("decision must be accept or reject")
        key = f"kernel:proposal:{card_id}:{seq}"
        with self.db.sessions() as s:
            self.goal_card(s, goal_id, card_id)
            if s.scalar(select(CardEvent.id).where(CardEvent.idempotency_key == key)):
                raise Conflict("该提议已经决定过")
            event = s.scalar(
                select(CardEvent).where(
                    CardEvent.card_id == card_id, CardEvent.seq == seq, CardEvent.kind == "proposal"
                )
            )
            if not event:
                raise ValueError("proposal not found")
            proposal = dict(event.data.get("proposal") or {})
        note = str(args.get("note", ""))[:2000]
        created = None
        if decision == "accept":
            created = self.tool_card_create(
                goal_id,
                {
                    "title": proposal.get("title", ""),
                    "goal": proposal.get("goal", ""),
                    "acceptance": proposal.get("acceptance", ""),
                    "bot": args.get("bot") or proposal.get("bot") or None,
                    "tags": proposal.get("tags", []),
                    # 提议多是原卡的后续：默认依赖原卡，新工作区经 inputs/ 拿到原卡产物。
                    "depends_on": [card_id] if args.get("after_source", True) is not False else [],
                },
            )
            body = f"提议「{proposal.get('title', '')}」已采纳，新卡片 {created['card_id']}"
        else:
            body = f"提议「{proposal.get('title', '')}」未采纳"
        self.cards.comment(card_id, body + (f"：{note}" if note else ""), actor=ACTOR, key=key)
        return {"decision": decision, **({"card_id": created["card_id"]} if created else {})}

    def tool_card_comment(self, goal_id, args):
        body = str(args.get("body", "")).strip()[:16000]
        if not body:
            raise ValueError("body required")
        with self.db.sessions() as s:
            card = self.goal_card(s, goal_id, args.get("card_id"))
            blocked = card.status == "BLOCKED"
        if blocked:
            card = self.cards.action(card.id, "answer", None, body, ACTOR)
            return {"card_id": card["id"], "status": card["status"], "answered": True}
        self.cards.comment(card.id, body, actor=ACTOR)
        return {"card_id": card.id, "commented": True}

    def say(self, goal_id, kind, text):
        text = str(text).strip()[:16000]
        if not text:
            raise ValueError("text required")
        with self.db.sessions.begin() as s:
            conversation = lock_conversation(s, goal_id)
            message = append_message(
                s,
                conversation,
                role="assistant",
                source="KERNEL",
                kind=kind,
                content=text,
                status="COMPLETED",
            )
            return conversation, message.id

    def tool_ask_user(self, goal_id, args):
        _, message_id = self.say(goal_id, "QUESTION", args.get("question", ""))
        return {"message_id": message_id}

    def tool_goal_reply(self, goal_id, args):
        final = bool(args.get("final"))
        _, message_id = self.say(goal_id, "FINAL" if final else "REPLY", args.get("text", ""))
        if final:
            with self.db.sessions.begin() as s:
                conversation = lock_conversation(s, goal_id)
                conversation.settings = {**(conversation.settings or {}), "goal_status": "DONE"}
                cards = list(s.scalars(select(Card).where(Card.conversation_id == goal_id)))
                lines = [f"- {c.title}：{c.status}" for c in cards][:50]
                remember(
                    s,
                    "goal",
                    goal_id,
                    f"Goal：{conversation.title}\n{str(args.get('text', ''))[:8000]}\n卡片：\n"
                    + "\n".join(lines),
                    tags=["goal"],
                )
                emit(s, "goal.completed", conversation_id=goal_id)
        return {"message_id": message_id, "final": final}

    def tool_memory_search(self, goal_id, args):
        return {"memories": search_memories(self.db, str(args.get("query", "")), args.get("kind"), 10)}

    def tool_artifact_read(self, goal_id, args):
        with self.db.sessions() as s:
            card = self.goal_card(s, goal_id, args.get("card_id"))
            files = list(card.result.get("files", []))
        if not args.get("path"):
            return {"files": [{"path": f.get("path"), "size": f.get("size")} for f in files][:100]}
        match = next((f for f in files if f.get("path") == args["path"]), None)
        if not match:
            raise ValueError("file not found in card result")
        if "text" in match:
            text = str(match["text"])
        elif "base64" in match:
            raw = base64.b64decode(match["base64"])
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                return {"path": match["path"], "binary": True, "size": len(raw)}
        else:
            return {"path": match["path"], "content": None, "note": "文件未随结果内联"}
        return {"path": match["path"], "content": text[:20000], "truncated": len(text) > 20000}


TOOL_NAMES = {t["function"]["name"] for t in TOOLS}


def goal_status(conversation):
    return (conversation.settings or {}).get("goal_status", "ACTIVE")


def parse_args(call):
    raw = call.get("function", {}).get("arguments")
    try:
        value = json.loads(raw) if isinstance(raw, str) and raw.strip() else (raw or {})
    except json.JSONDecodeError:
        return {"raw": str(raw)[:500]}
    return value if isinstance(value, dict) else {"raw": str(value)[:500]}


def clip(value, limit):
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + "…(truncated)"


def remember(s, kind, ref_id, content, bot_id=None, tags=()):
    """按 (kind, ref_id) 覆盖写入一条白板记忆。"""
    memory = s.scalar(select(Memory).where(Memory.kind == kind, Memory.ref_id == ref_id).limit(1))
    if memory is None:
        memory = Memory(kind=kind, ref_id=ref_id)
        s.add(memory)
    memory.content, memory.bot_id, memory.tags = content[:16000], bot_id, list(tags)[:20]
    memory.created_at = now()
    s.flush()
    emit(s, "memory.created", memory_id=memory.id, kind=kind)
    return memory


def search_memories(db, query, kind=None, limit=10):
    words = [w for w in query.split() if w][:5]
    with db.sessions() as s:
        statement = select(Memory).order_by(Memory.created_at.desc()).limit(limit)
        if kind:
            statement = statement.where(Memory.kind == kind)
        if words:
            statement = statement.where(or_(*[Memory.content.ilike(f"%{w}%") for w in words]))
        return [as_dict(m) for m in s.scalars(statement)]
