import type {
  Action,
  Approval,
  Computer,
  Task,
  TaskOperation,
  TaskProgress,
} from "./types";

export const phaseLabels: Record<string, string> = {
  QUEUED: "已排队",
  PLANNING: "规划任务",
  MODEL_REQUEST: "等待模型",
  PROCESSING: "处理中",
  WAITING_REMOTE: "等待远程 Pi",
  EXECUTING_ACTION: "执行动作",
  RECOVERING: "恢复会话",
  WAITING_APPROVAL: "等待审批",
  WAITING_RECONCILIATION: "等待核对",
  WAITING_COMPUTER: "等待电脑",
  WAITING_HUMAN: "等待协助",
  PAUSED: "已暂停",
  COMPLETED: "已完成",
  CANCELLED: "已停止",
  FAILED: "执行失败",
};

// Old Kernels expose only status/checkpoint. Never infer a model stage from
// action count, and keep approval/UNKNOWN separate from ordinary resume.
export function taskProgress(
  task: Task,
  actions: Action[] = [],
  approvals: Approval[] = [],
  computer?: Computer,
): TaskProgress {
  if (task.progress) return task.progress;
  const operations: TaskOperation[] = [];
  const terminal = ["COMPLETED", "CANCELLED"].includes(task.status);
  const pending = approvals.some((a) => a.action.task_id === task.id);
  const unknown =
    actions.some((a) => a.status === "UNKNOWN") ||
    task.checkpoint.reason === "UNKNOWN";
  const blocked =
    pending ||
    unknown ||
    task.checkpoint.reason === "WAITING_APPROVAL" ||
    (task.checkpoint.reason === "computer required" && !task.computer_id) ||
    (computer && computer.control !== "AGENT");
  if (!terminal) {
    operations.push("cancel");
    if (["PENDING", "RUNNING", "WAITING_HUMAN"].includes(task.status))
      operations.push("pause");
    if (["PAUSED", "WAITING_HUMAN"].includes(task.status) && !blocked)
      operations.push("resume");
    if (task.status === "FAILED" && !blocked) operations.push("retry");
    if (
      !task.computer_id &&
      ["PENDING", "PAUSED", "WAITING_HUMAN"].includes(task.status)
    )
      operations.push("attach_computer");
    if (pending) operations.push("approve", "deny");
    if (unknown) operations.push("reconcile");
  }
  let phase: string = task.status;
  let message = terminal
    ? task.status === "COMPLETED"
      ? "任务已完成"
      : "任务已停止"
    : task.status === "PENDING"
      ? "任务已排队，将自动开始"
      : task.status === "PAUSED"
        ? "任务已暂停，可以恢复或停止"
        : "当前 Kernel 未提供详细执行阶段";
  let waitReason = terminal ? null : task.checkpoint.reason || null;
  if (!terminal && task.status !== "FAILED") {
    if (unknown) {
      phase = "WAITING_RECONCILIATION";
      message = "动作结果不确定，请先核对实际结果";
    } else if (pending || task.checkpoint.reason === "WAITING_APPROVAL") {
      phase = "WAITING_APPROVAL";
      message = "等待你批准或拒绝动作，恢复任务不能代替审批";
    } else if (computer && computer.control !== "AGENT") {
      phase = "WAITING_COMPUTER";
      waitReason =
        computer.control === "HUMAN"
          ? "COMPUTER_HUMAN_CONTROL"
          : "COMPUTER_PAUSED";
      message =
        computer.control === "HUMAN"
          ? "执行电脑由你控制，请先交还 Agent；任务不会自动开始"
          : "执行电脑已暂停，请先恢复电脑";
    }
    if (task.status === "PAUSED" && phase !== "PAUSED")
      message = "任务已暂停；" + message;
  }
  return {
    phase,
    message,
    wait_reason: waitReason,
    last_progress_at: "",
    allowed_operations: operations,
    action_id: null,
  };
}
