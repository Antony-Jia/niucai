"""Task progress projection shared by REST, durable events and control guards.

The small journal in checkpoint needs no schema migration. Business events advance
its clock; lease renewal deliberately does not. Existing checkpoints remain readable.
"""

from contextvars import ContextVar

from sqlalchemy import select

from niucai.storage.db import Action, Computer, Task, as_dict, now

run_context = ContextVar("task_run", default=None)
TERMINAL = {"COMPLETED", "FAILED", "CANCELLED"}
WAIT_FIELDS = {"reason", "detail", "action_id", "paused_by_computer"}


def clear_wait(checkpoint, *, failed=False):
    value = {k: v for k, v in checkpoint.items() if k not in WAIT_FIELDS}
    if failed and checkpoint.get("detail"):
        value["error_detail"] = checkpoint["detail"]
        value["detail"] = checkpoint["detail"]  # Preserve the old failure-detail contract.
    return value


def task_progress(session, task):
    journal = task.checkpoint.get("_progress", {})
    timestamp = journal.get("last_progress_at") or task.created_at.isoformat() + "Z"
    result = {
        "phase": task.status,
        "message": "",
        "wait_reason": None,
        "last_progress_at": timestamp,
        "allowed_operations": [],
        "action_id": None,
    }
    if task.status in {"COMPLETED", "CANCELLED"}:
        result["message"] = (
            "任务已完成" if task.status == "COMPLETED" else "任务已停止，已完成的动作和产物保留"
        )
        return result
    actions = session.scalars(
        select(Action)
        .where(Action.task_id == task.id, Action.status.in_(["UNKNOWN", "WAITING_APPROVAL"]))
        .order_by(Action.created_at, Action.id)
    ).all()
    unknown = next((a for a in actions if a.status == "UNKNOWN"), None)
    approval = next((a for a in actions if a.status == "WAITING_APPROVAL"), None)
    computer = session.get(Computer, task.computer_id) if task.computer_id else None
    reason = task.checkpoint.get("reason")
    operations = ["cancel"]
    if task.status in {"PENDING", "RUNNING", "WAITING_HUMAN"}:
        operations.insert(0, "pause")
    if not task.computer_id and task.status in {"PENDING", "PAUSED", "WAITING_HUMAN"}:
        operations.append("attach_computer")
    blocked = bool(unknown or approval or (computer and computer.control != "AGENT"))
    blocked = blocked or (reason == "computer required" and not task.computer_id)
    if task.status == "FAILED":
        if (
            not unknown
            and not (computer and computer.control != "AGENT")
            and not (reason == "computer required" and not task.computer_id)
        ):
            operations.insert(0, "retry")
    elif task.status in {"PAUSED", "WAITING_HUMAN"} and not blocked:
        operations.insert(0, "resume")
    if approval and task.status != "FAILED":
        operations.extend(["approve", "deny"])
    if unknown:
        operations.append("reconcile")
    result["allowed_operations"] = operations

    def waiting(phase, code, message, action=None):
        result.update(phase=phase, wait_reason=code, message=message, action_id=action.id if action else None)

    if task.status == "FAILED":
        waiting("FAILED", "TASK_FAILED", "执行失败，请查看错误详情；满足条件后可重试")
        if unknown:
            waiting("FAILED", "ACTION_UNKNOWN", "执行失败，仍有结果不确定的动作；请先核对再重试", unknown)
        elif computer and computer.control != "AGENT":
            waiting(
                "FAILED",
                "COMPUTER_HUMAN_CONTROL" if computer.control == "HUMAN" else "COMPUTER_PAUSED",
                "执行失败；请先交还或恢复执行电脑，再重试任务",
            )
        elif approval:
            waiting(
                "FAILED", "APPROVAL_REQUIRED", "执行失败；重试会恢复到审批等待，不会自动执行动作", approval
            )
    elif unknown:
        waiting(
            "WAITING_RECONCILIATION", "ACTION_UNKNOWN", "动作结果不确定，请查看设备并核对实际结果", unknown
        )
    elif approval:
        waiting(
            "WAITING_APPROVAL", "APPROVAL_REQUIRED", "等待你批准或拒绝动作，继续任务不能代替审批", approval
        )
    elif reason == "computer required" and not task.computer_id:
        waiting("WAITING_COMPUTER", "COMPUTER_REQUIRED", "请选择并绑定执行电脑，再继续任务")
    elif computer and computer.control != "AGENT":
        human = computer.control == "HUMAN"
        waiting(
            "WAITING_COMPUTER",
            "COMPUTER_HUMAN_CONTROL" if human else "COMPUTER_PAUSED",
            "电脑由你控制，请先交还 Agent" if human else "电脑已暂停，请先恢复电脑",
        )
    elif task.status == "PAUSED":
        waiting("PAUSED", "USER_PAUSED", "任务已暂停，可以继续或停止")
    elif task.status == "WAITING_HUMAN":
        waits = {
            "step budget exhausted": ("STEP_BUDGET_EXHAUSTED", "动作预算已用尽，继续将增加一轮动作预算"),
            "model turn budget exhausted": ("MODEL_BUDGET_EXHAUSTED", "模型轮次预算已用尽，可继续当前会话"),
            "graph budget exhausted": ("GRAPH_BUDGET_EXHAUSTED", "推理预算已用尽，可继续当前会话"),
            "agent requested human": ("HUMAN_REQUESTED", "Agent 需要你的协助，处理后继续任务"),
        }
        code, message = waits.get(reason, ("HUMAN_REQUESTED", "任务等待人工处理，处理后可继续"))
        waiting("WAITING_HUMAN", code, message)
    elif task.status == "PENDING":
        result.update(phase="QUEUED", message="任务已排队，将自动开始")
        if computer and computer.active_task_id and computer.active_task_id != task.id:
            other = session.get(Task, computer.active_task_id)
            if other and other.status == "RUNNING" and other.lease_until and other.lease_until > now():
                waiting("QUEUED", "COMPUTER_BUSY", "执行电脑正在处理另一任务，完成后自动开始")
    else:
        phases = {
            "PLANNING": "正在规划任务",
            "MODEL_REQUEST": "正在等待模型响应",
            "EXECUTING_ACTION": "正在执行动作",
            "RECOVERING": "正在恢复上一次执行会话",
            "PROCESSING": "Agent 正在处理当前步骤",
        }
        phase = journal.get("phase", "PROCESSING")
        result.update(
            phase=phase if phase in phases else "PROCESSING", message=phases.get(phase, phases["PROCESSING"])
        )
        if result["phase"] == "EXECUTING_ACTION":
            result["action_id"] = journal.get("action_id")
    if task.status == "PAUSED" and result["phase"] != "PAUSED":
        result["message"] = "任务已暂停；" + result["message"]
    return result


def task_snapshot(session, task):
    value = as_dict(task)
    if task.status in TERMINAL:
        value["checkpoint"] = clear_wait(task.checkpoint, failed=task.status == "FAILED")
    return {**value, "progress": task_progress(session, task)}


def journal_event(session, task, event_type, data):
    """Called in the same transaction as each business event, never heartbeat."""
    if event_type.startswith("model.") and run_context.get() != (task.id, task.run_token):
        return False  # A late response cannot advance a new run or a paused task.
    phases = {
        "task.started": "RECOVERING" if data.get("recovered") else "PROCESSING",
        "task.recovered": "RECOVERING",
        "task.plan_updated": "PROCESSING",
        "task.step_completed": "PROCESSING",
        "action.executing": "EXECUTING_ACTION",
        "model.requested": "PLANNING" if data.get("role") == "planner" else "MODEL_REQUEST",
        "model.completed": "PROCESSING",
        "model.failed": "PROCESSING",
    }
    journal = {**task.checkpoint.get("_progress", {}), "last_progress_at": now().isoformat() + "Z"}
    if event_type in phases:
        journal["phase"] = phases[event_type]
    if event_type == "task.phase_changed":
        journal["phase"] = data["phase"]
    if event_type == "action.executing":
        journal["action_id"] = data["action_id"]
    elif event_type.startswith("action."):
        journal.pop("action_id", None)
        if event_type not in {"action.proposed", "action.approved", "action.denied"}:
            journal["phase"] = "PROCESSING"
    task.checkpoint = {**task.checkpoint, "_progress": journal}
    return True
