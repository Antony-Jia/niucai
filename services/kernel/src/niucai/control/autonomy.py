"""会话级执行模式与 YOLO 免审批。

YOLO 是会话属性，委派出的子会话继承来源会话的设置；Kernel 总开关
`NIUCAI_YOLO_ENABLED` 关闭时一律视为未开启。shell.exec 永远需要人工审批。
"""

from niucai.control.tasks import Conflict
from niucai.storage.db import Conversation, Task

SCOPES = ("actions", "remote")
MODES = ("agent", "chat")
# 本机 shell 不是安全沙箱，YOLO 不覆盖。
NEVER_AUTO = {"shell.exec"}


def normalize(value, settings=None):
    """校验并补全会话设置；开启任一 YOLO 范围需要 Kernel 总开关。"""
    value = dict(value or {})
    unknown = set(value) - {"mode", "autonomy"}
    if unknown:
        raise ValueError(f"unknown conversation settings: {', '.join(sorted(unknown))}")
    mode = value.get("mode", "agent")
    if mode not in MODES:
        raise ValueError("mode must be agent or chat")
    autonomy = dict(value.get("autonomy") or {})
    if set(autonomy) - set(SCOPES):
        raise ValueError("autonomy scopes are actions and remote")
    autonomy = {scope: bool(autonomy.get(scope, False)) for scope in SCOPES}
    if settings is not None and any(autonomy.values()) and not settings.yolo_enabled:
        raise Conflict("YOLO 模式未在 Kernel 启用（NIUCAI_YOLO_ENABLED）")
    return {"mode": mode, "autonomy": autonomy}


def conversation_settings(session, conversation_id):
    conversation = session.get(Conversation, conversation_id) if conversation_id else None
    return normalize(conversation.settings if conversation else {})


def task_settings(session, task):
    if isinstance(task, str):
        task = session.get(Task, task)
    return conversation_settings(session, task.conversation_id if task else None)


def yolo(session, settings, task, scope):
    """当前任务所在会话是否对该范围免审批。"""
    if not settings.yolo_enabled:
        return False
    return task_settings(session, task)["autonomy"][scope]


def inherit(session, source_conversation_id, target):
    """委派/流程阶段的执行会话继承来源会话的模式与 YOLO。"""
    source = session.get(Conversation, source_conversation_id) if source_conversation_id else None
    if source and source.settings:
        target.settings = normalize(source.settings)
