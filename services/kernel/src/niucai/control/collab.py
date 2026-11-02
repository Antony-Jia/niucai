"""Bot 协作 C1 的纯逻辑：环境画像、资源声明与冲突判断、白板产物。

设计见 docs/bot-delegation-and-discussion.md §6、§7、§10。这里不开事务，调用方持有会话与卡片锁。
"""

import base64
import binascii
import fnmatch
import hashlib
import re
from datetime import timedelta

from sqlalchemy import func, select

from niucai.control.tasks import Conflict
from niucai.storage.db import BoardArtifact, Bot, Card, now

# ---- 环境画像 -------------------------------------------------------------------

# 手填部分只写名称与说明，不存凭据；界面上也有同样的提示。
MANUAL_LIMITS = {"logged_in_sites": (30, 100), "resources": (30, 200)}
NOTES_LIMIT = 2000
USERINFO = re.compile(r"(?<=://)[^/@\s]+@")


def clean_text(value, limit):
    return str(value).strip()[:limit] if value is not None else ""


def clean_list(value, count, limit):
    if not isinstance(value, list):
        return []
    return [text for item in value[:count] if (text := clean_text(item, limit))]


def clean_manual(value):
    if not isinstance(value, dict):
        raise ValueError("environment must be an object")
    unknown = set(value) - {*MANUAL_LIMITS, "notes"}
    if unknown:
        raise ValueError(f"unknown environment fields: {', '.join(sorted(unknown))}")
    return {
        **{key: clean_list(value.get(key), *limits) for key, limits in MANUAL_LIMITS.items()},
        "notes": clean_text(value.get("notes"), NOTES_LIMIT),
    }


def redact_remote(url):
    """远端地址里可能带 https://user:token@host：只保留主机与路径。"""
    return USERINFO.sub("", clean_text(url, 300))


def clean_probe(raw):
    """Runner 上报的探测结果：只保留已知字段并限长；节点是半可信的，不原样落库。"""
    if not isinstance(raw, dict):
        return {}
    engines = {}
    for name, value in list((raw.get("engines") or {}).items())[:8]:
        if isinstance(value, dict):
            engines[clean_text(name, 20)] = {
                "available": bool(value.get("available")),
                "version": clean_text(value.get("version"), 60),
            }
    browser = raw.get("browser") if isinstance(raw.get("browser"), dict) else {}
    repos = []
    for item in (raw.get("repos") or [])[:20]:
        if isinstance(item, dict):
            repos.append(
                {"name": clean_text(item.get("name"), 100), "remote": redact_remote(item.get("remote"))}
            )
    return {
        "os": clean_text(raw.get("os"), 200),
        "engines": engines,
        "browser": {"available": bool(browser.get("available")), "cdp": bool(browser.get("cdp"))},
        "repos": repos,
        "cli": clean_list(raw.get("cli"), 20, 40),
    }


def apply_probe(s, node_id, raw):
    """心跳时把节点探测结果合进该节点所有 Bot 的 environment.probed；内容没变则不写。"""
    probe = clean_probe(raw)
    if not probe:
        return
    for bot in s.scalars(select(Bot).where(Bot.node_id == node_id)):
        current = dict(bot.environment or {})
        if current.get("probed") != probe:
            bot.environment = {**current, "probed": probe}


def environment_view(bot):
    """给同事看的环境摘要：探测部分只带本 Bot 引擎的版本，手填部分全量。"""
    environment = bot.environment or {}
    probe = dict(environment.get("probed") or {})
    engines = probe.pop("engines", {})
    if probe or engines:
        probe["engine"] = {"name": bot.engine, **engines.get(bot.engine, {})}
    manual = environment.get("manual") or {}
    return {"probed": probe, "manual": manual}


# ---- 资源声明 -------------------------------------------------------------------

RESOURCE = re.compile(r"(repo|path|site|desktop|browser|ext):(\S{1,190})")
EXCLUSIVE_DEFAULT = {"site", "desktop", "browser"}
MODES = ("shared", "exclusive")
CLAIM_LIMIT = 20
# exclusive 只在 Attempt 执行期间持有；shared 在卡片被阻塞/待验收时仍显示（工作未合入）。
EXCLUSIVE_HOLD = {"RUNNING"}
SHARED_HOLD = {"RUNNING", "BLOCKED", "REVIEW"}


def normalize_claims(raw):
    """校验并规范化资源声明；同一资源重复时取更严的模式。"""
    if not isinstance(raw, list):
        raise ValueError("claims must be a list")
    result = {}
    for item in raw:
        entry = {"resource": item} if isinstance(item, str) else item
        if not isinstance(entry, dict):
            raise ValueError("claim must be an object")
        resource = str(entry.get("resource", "")).strip()
        match = RESOURCE.fullmatch(resource)
        if not match:
            raise ValueError(
                f"invalid resource id: {resource[:60]!r} (use repo:/path:/site:/desktop:/browser:/ext:<id>)"
            )
        kind = match.group(1)
        # 站点、桌面、浏览器的标识大小写无关；仓库与路径区分大小写。
        if kind in EXCLUSIVE_DEFAULT:
            resource = resource.lower()
        mode = entry.get("mode") or ("exclusive" if kind in EXCLUSIVE_DEFAULT else "shared")
        if mode not in MODES:
            raise ValueError(f"claim mode must be shared or exclusive, got {mode!r}")
        if result.get(resource) == "exclusive":
            mode = "exclusive"
        result[resource] = mode
    if len(result) > CLAIM_LIMIT:
        raise ValueError(f"at most {CLAIM_LIMIT} claims per card")
    return [{"resource": r, "mode": m} for r, m in result.items()]


def overlaps(a, b):
    if a == b:
        return True
    if not (a.startswith("path:") and b.startswith("path:")):
        return False
    # path:<remote>:<glob>，远端地址自身可能含冒号，所以从右侧切出 glob。
    (ra, ga), (rb, gb) = a[5:].rsplit(":", 1), b[5:].rsplit(":", 1)
    return ra == rb and (fnmatch.fnmatchcase(ga, gb) or fnmatch.fnmatchcase(gb, ga))


def other_claims(s, card):
    """其它未结束且有声明的卡片；声明跨 Goal 生效（资源是环境本身）。"""
    return [
        c
        for c in s.scalars(
            select(Card).where(Card.id != card.id, Card.status.in_(SHARED_HOLD)).order_by(Card.created_at)
        )
        if c.claims
    ]


def holder_text(s, card, holder, resource):
    """占用者描述；与 bots_list 一致，别的 Goal 的卡片只说明存在，不露 ID 与标题。"""
    bot = s.get(Bot, holder.assignee_bot_id) if holder.assignee_bot_id else None
    since = holder.updated_at.strftime("%H:%M") if holder.updated_at else "?"
    who = (
        f"卡片 {holder.id}《{holder.title}》"
        if holder.conversation_id == card.conversation_id
        else "其它 Goal 的卡片"
    )
    return f"{resource} 由{who}（{holder.status}{'，Bot ' + bot.name if bot else ''}，自 {since} 起）持有"


def claim_conflicts(s, card, wanted):
    """返回 (exclusive 冲突, shared 提示)；两者都是文本列表，同一 Goal 的占用卡片信息已在文本里。"""
    conflicts, hints = [], []
    for holder in other_claims(s, card):
        for claim in wanted:
            for held in holder.claims:
                if not overlaps(claim["resource"], held["resource"]):
                    continue
                hard = "exclusive" in (claim["mode"], held["mode"])
                text = holder_text(s, card, holder, held["resource"])
                if hard and holder.status in EXCLUSIVE_HOLD:
                    conflicts.append(text)
                else:
                    hints.append(text)
    return sorted(set(conflicts)), sorted(set(hints))


def merge_claims(current, wanted):
    merged = {c["resource"]: c["mode"] for c in current or []}
    for claim in wanted:
        merged[claim["resource"]] = (
            "exclusive" if "exclusive" in (merged.get(claim["resource"]), claim["mode"]) else "shared"
        )
    return [{"resource": r, "mode": m} for r, m in merged.items()]


# ---- 白板产物 -------------------------------------------------------------------

ARTIFACT_MAX = 1024 * 1024  # 单个产物上限（字节）；更大的东西走 git 或共享存储，委派里只给引用
ARTIFACT_INPUT_BUDGET = 4 * 1024 * 1024  # 一张卡 inputs 里产物的总字节上限（随领取内联下发）
GOAL_ARTIFACT_COUNT, GOAL_ARTIFACT_BYTES = 100, 64 * 1024 * 1024
ARTIFACT_NAME = re.compile(r"[A-Za-z0-9_一-鿿][A-Za-z0-9_.\- 一-鿿]{0,199}")


def artifact_name(value):
    name = str(value or "").strip()
    if not ARTIFACT_NAME.fullmatch(name) or name.endswith(".") or ".." in name:
        raise ValueError("artifact name must be a plain file name (letters, digits, - _ . and spaces)")
    return name


def artifact_bytes(args):
    if "base64" in args:
        try:
            return base64.b64decode(str(args["base64"]), validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("artifact base64 is invalid") from None
    if "text" in args:
        return str(args["text"]).encode()
    raise ValueError("artifact content required (text or base64)")


def store_artifact(s, card, bot_id, name, data):
    name = artifact_name(name)
    if len(data) > ARTIFACT_MAX:
        raise ValueError(
            f"产物 {len(data)} 字节，超过单个上限 {ARTIFACT_MAX} 字节；"
            "大文件请走 git 或共享存储，在委派/评论里只给引用"
        )
    digest = hashlib.sha256(data).hexdigest()
    existing = s.scalar(
        select(BoardArtifact).where(
            BoardArtifact.card_id == card.id,
            BoardArtifact.name == name,
            BoardArtifact.content_hash == digest,
        )
    )
    if existing:
        return existing, False  # Runner 转发重试时同一产物只存一份
    count, total = s.execute(
        select(func.count(BoardArtifact.id), func.coalesce(func.sum(BoardArtifact.size), 0)).where(
            BoardArtifact.conversation_id == card.conversation_id
            if card.conversation_id
            else BoardArtifact.conversation_id.is_(None)
        )
    ).one()
    if count >= GOAL_ARTIFACT_COUNT or total + len(data) > GOAL_ARTIFACT_BYTES:
        raise Conflict("本 Goal 的产物数量或总大小已达上限")
    artifact = BoardArtifact(
        conversation_id=card.conversation_id,
        card_id=card.id,
        bot_id=bot_id,
        name=name,
        size=len(data),
        content_hash=digest,
        content=data,
    )
    s.add(artifact)
    s.flush()
    return artifact, True


def goal_artifact(s, conversation_id, ident):
    artifact = s.get(BoardArtifact, str(ident or ""))
    if artifact is None:
        raise ValueError("artifact not found")
    if artifact.conversation_id != conversation_id:
        raise PermissionError("artifact belongs to another goal")
    return artifact


def normalize_inputs(s, conversation_id, raw):
    """卡片 inputs：[{"artifact_id": ...}]；产物须在同一 Goal 内，总大小不超过内联预算。"""
    if not isinstance(raw, list):
        raise ValueError("inputs must be a list")
    seen, result, total = set(), [], 0
    for item in raw:
        ident = item.get("artifact_id") if isinstance(item, dict) else item
        artifact = goal_artifact(s, conversation_id, ident)
        if artifact.id in seen:
            continue
        seen.add(artifact.id)
        total += artifact.size
        result.append({"artifact_id": artifact.id})
    if total > ARTIFACT_INPUT_BUDGET:
        raise ValueError(f"输入产物合计 {total} 字节，超过上限 {ARTIFACT_INPUT_BUDGET} 字节")
    if len(result) > 32:
        raise ValueError("at most 32 input artifacts per card")
    return result


def artifact_inputs(s, card):
    """领取时下发：按来源卡片分组，Runner 写入 inputs/<来源卡片 id>/<产物名>。"""
    groups = {}
    for item in card.inputs or []:
        artifact = s.get(BoardArtifact, item.get("artifact_id"))
        if artifact is None:
            continue
        group = groups.setdefault(artifact.card_id, [])
        group.append(
            {
                "path": artifact.name,
                "base64": base64.b64encode(artifact.content).decode(),
                "artifact_id": artifact.id,
            }
        )
    result = []
    for source, files in groups.items():
        title = s.scalar(select(Card.title).where(Card.id == source)) or source
        result.append(
            {
                "card_id": source,
                "title": title,
                "summary": "",
                "files": files,
                "artifacts": [f["path"] for f in files],
            }
        )
    return result


def bot_online(node, settings):
    return bool(
        node
        and node.enabled
        and node.heartbeat_at
        and node.heartbeat_at > now() - timedelta(seconds=settings.remote_lease_seconds)
    )


def bot_state(online, running, concurrency):
    if not online:
        return "offline"
    return "idle" if running == 0 else ("busy" if running < concurrency else "full")
