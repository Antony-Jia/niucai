"""任务收尾报告：把一轮执行整理成结论、访问过的页面和产出的文件。

事实只来自 Kernel 自己记录的 Action 与 Artifact，模型只负责把事实写成人话；
没有配置 summarizer 或模型调用失败时，使用确定性的文字，保证用户总能看到结果。
"""

import asyncio
import json
import logging
import re

from sqlalchemy import select

from niucai.domain.schemas import Role
from niucai.storage.db import Action, Artifact, Task

log = logging.getLogger("niucai.report")

BUDGET_REASONS = {"model turn budget exhausted", "step budget exhausted"}
EXCERPT = 1500
DOWNLOAD_TOOLS = {"curl", "wget", "aria2c", "git", "pip", "npm", "yt-dlp"}
REPORT_PROMPT = (
    "你是 niucai 的任务收尾助手。根据用户目标和 Kernel 记录的事实，用中文写一份简短的执行总结。"
    "只能使用给出的事实，不要编造页面、数字或文件。结构：先一句话结论（是否完成目标、查到了什么）；"
    "再列关键发现（来自页面内容摘录）；最后说明未完成的部分和建议。不要使用文件清单，文件由系统追加。"
)


def collect(db, task_id):
    """读取一个任务的动作与产出，返回可序列化的事实。"""
    with db.sessions() as session:
        actions = list(
            session.scalars(select(Action).where(Action.task_id == task_id).order_by(Action.created_at))
        )
        artifacts = list(
            session.scalars(select(Artifact).where(Artifact.task_id == task_id).order_by(Artifact.created_at))
        )
        visited, downloads, written, failures, excerpts = [], [], [], [], []
        for action in actions:
            spec, result = action.spec or {}, action.result or {}
            kind = spec.get("type", "")
            if action.status == "FAILED" or (action.status == "UNKNOWN"):
                failures.append(f"{kind}: {str(result.get('error', action.status))[:200]}")
            if kind == "browser.navigate" and spec.get("url") not in visited:
                visited.append(str(spec["url"]))
            if kind == "browser.snapshot" and result.get("text"):
                title = str(result.get("title") or result.get("url") or "")
                excerpts.append({"title": title, "url": result.get("url", ""), "text": result["text"]})
            if kind == "files.write" and spec.get("path") not in written:
                written.append(spec["path"])
            if kind == "shell.exec":
                argv = [str(a) for a in spec.get("argv", [])]
                if argv and argv[0].rsplit("/", 1)[-1] in DOWNLOAD_TOOLS:
                    downloads.append(
                        {
                            "command": " ".join(argv)[:300],
                            "exit_code": result.get("exit_code"),
                        }
                    )
        files = [{"path": a.path, "media_type": a.media_type, "id": a.id} for a in artifacts]
        return {
            "actions": len(actions),
            "visited": visited[:30],
            "files": files[:50],
            "written": written[:50],
            "downloads": downloads[:20],
            "failures": failures[-5:],
            "excerpts": excerpts[-3:],
        }


def files_section(facts):
    lines = []
    if facts["files"]:
        lines.append("**产出的文件**")
        lines += [f"- `{f['path']}`（{f['media_type']}）" for f in facts["files"]]
    shown = {f["path"] for f in facts["files"]}
    extra = [p for p in facts["written"] if p not in shown]
    if extra:
        lines.append("**写入的文件**")
        lines += [f"- `{p}`" for p in extra]
    if facts["downloads"]:
        lines.append("**下载/安装类命令**")
        for item in facts["downloads"]:
            code = item["exit_code"]
            state = "成功" if code == 0 else f"退出码 {code}" if code is not None else "结果未知"
            lines.append(f"- `{item['command']}`（{state}）")
    return "\n".join(lines)


def pages_section(facts):
    if not facts["visited"]:
        return ""
    return "**访问的页面**\n" + "\n".join(f"- {url}" for url in facts["visited"][:10])


def fallback(checkpoint, status, reason, facts):
    """没有模型可用时的确定性结论。"""
    if status == "COMPLETED":
        head = "任务已完成，但执行过程没有给出文字结论。"
    elif status == "FAILED":
        detail = checkpoint.get("error_detail") or checkpoint.get("detail") or ""
        head = f"任务执行失败：{str(detail)[:300] or '未知错误'}。"
    elif reason in BUDGET_REASONS:
        head = (
            "执行次数已达到上限，任务暂停，尚未给出最终结论。"
            "以下是目前的进展，可点击“继续”让 Agent 接着完成。"
        )
    else:
        head = "任务暂停，等待处理。"
    parts = [head, f"共执行 {facts['actions']} 个动作。" if facts["actions"] else ""]
    if facts["excerpts"]:
        last = facts["excerpts"][-1]
        parts.append(f"最近查看的页面：{last['title'] or last['url']}\n> " + _squash(last["text"], 400))
    if facts["failures"]:
        parts.append("遇到的问题：\n" + "\n".join(f"- {f}" for f in facts["failures"]))
    return "\n\n".join(p for p in parts if p)


def _squash(text, limit):
    return re.sub(r"\s+", " ", str(text)).strip()[:limit]


async def conclude(gateway, task, status, reason, answer, facts):
    """请 summarizer 写结论；失败时返回 None，由调用方使用 fallback。"""
    if gateway is None:
        return None
    payload = {
        "goal": task.goal,
        "status": status,
        "stop_reason": reason,
        "final_answer": answer[:3000],
        "visited": facts["visited"],
        "failures": facts["failures"],
        "page_excerpts": [
            {"title": e["title"], "url": e["url"], "text": _squash(e["text"], EXCERPT)}
            for e in facts["excerpts"]
        ],
    }
    try:
        data = await gateway.chat(
            Role.SUMMARIZER,
            [
                {"role": "system", "content": REPORT_PROMPT},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            task.id,
        )
        text = (data["choices"][0]["message"].get("content") or "").strip()
        return text or None
    except Exception as exc:  # 报告是附加能力，不能让它拖垮任务收尾。
        log.warning("task %s report model unavailable: %s", task.id, type(exc).__name__)
        return None


async def build(worker, runtime, task, outcome):
    """补全 outcome：返回新的 (status, checkpoint)。不改变状态，只补充 result/report。"""
    status, checkpoint = outcome["status"], dict(outcome["checkpoint"])
    reason = checkpoint.get("reason")
    budget = status == "WAITING_HUMAN" and reason in BUDGET_REASONS
    if status not in {"COMPLETED", "FAILED"} and not budget:
        return outcome
    from niucai.control.tasks import require

    with worker.db.sessions() as session:
        current = require(session, Task, task.id)
    facts = await asyncio.to_thread(collect, worker.db, task.id)
    answer = str(checkpoint.get("result") or "").strip()
    substantive = status == "COMPLETED" and bool(answer)  # 有最终答复就保留，不额外调用模型。
    gateway = getattr(runtime, "gateway", None)
    conclusion = None
    if not substantive and status != "FAILED":  # 失败多半源于模型不可用，不再等待模型。
        try:
            await asyncio.to_thread(worker.tasks.phase, task.id, task.run_token, "MODEL_REQUEST")
        except Exception:
            pass
        conclusion = await conclude(gateway, current, status, reason, answer, facts)
    body = conclusion or (answer if substantive else None)
    if not body:
        body = fallback({**current.checkpoint, **checkpoint}, status, reason, facts)
    if answer and not substantive and conclusion is None and status == "COMPLETED":
        body = f"{answer}\n\n{body}"
    extras = [s for s in (pages_section(facts), files_section(facts)) if s]
    text = "\n\n".join([body, *extras])
    checkpoint["report"] = {
        "status": status,
        "reason": reason,
        "visited": facts["visited"],
        "files": facts["files"],
        "written": facts["written"],
        "downloads": facts["downloads"],
        "actions": facts["actions"],
        "failures": facts["failures"],
        "summary": text,
    }
    if status == "COMPLETED":
        checkpoint["result"] = text
    else:
        checkpoint["report_message"] = text
    return {"status": status, "checkpoint": checkpoint}
