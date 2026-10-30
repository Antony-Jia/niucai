"""删除已结束的任务：只做软删除，审计、动作、卡片与记忆记录保持不变。"""

from sqlalchemy import func, select

from niucai.control.conversations import lock_conversation
from niucai.control.progress import TERMINAL
from niucai.control.tasks import Conflict, require
from niucai.storage.db import Card, OrchestratorRun, Task, emit, now

CARD_CLOSED = ("DONE", "FAILED", "CANCELLED")


def remove_conversation(s, conversation_id):
    """软删除会话或 Goal；仍有未结束的任务、卡片或编排运行时拒绝，须先取消。"""
    conversation = lock_conversation(s, conversation_id)
    open_tasks = s.scalar(
        select(func.count(Task.id)).where(
            Task.conversation_id == conversation_id, Task.status.not_in(TERMINAL)
        )
    )
    open_cards = s.scalar(
        select(func.count(Card.id)).where(
            Card.conversation_id == conversation_id, Card.status.not_in(CARD_CLOSED)
        )
    )
    active_runs = s.scalar(
        select(func.count(OrchestratorRun.id)).where(
            OrchestratorRun.conversation_id == conversation_id,
            OrchestratorRun.status.in_(("PENDING", "RUNNING")),
        )
    )
    if open_tasks or open_cards or active_runs:
        raise Conflict("任务仍在进行，请先取消后再删除")
    conversation.deleted_at = now()
    emit(s, "item.removed", conversation_id=conversation_id)
    return conversation


def remove_task(s, task_id):
    """软删除独立任务；属于会话的任务随会话一起删除。"""
    task = require(s, Task, task_id, lock=True)
    if task.deleted_at:
        raise Conflict("任务已删除")
    if task.conversation_id:
        raise Conflict("该任务属于某个会话，请删除会话")
    if task.status not in TERMINAL:
        raise Conflict("任务仍在进行，请先取消后再删除")
    task.deleted_at = now()
    # 事件类型不带 task. 前缀：不写入任务进度日志，只通知前端刷新列表。
    emit(s, "item.removed", task.id)
    return task
