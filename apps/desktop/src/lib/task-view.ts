import type { Approval, Conversation, Task, TaskOperation } from "./types";
import type { GoalSummary } from "./goals";
import type { Bot } from "./board";
import type { Topology } from "../components/NodesPanel";

/**
 * 任务视图适配层：把会话、独立任务和 Goal 投影成同一份“我的任务”列表。
 * 只做展示投影，不混合后端 ID；每条记录都保留 sourceKind + sourceId。
 */
export type SourceKind = "conversation" | "task" | "goal";
export type DisplayStatus =
  | "queued"
  | "running"
  | "attention"
  | "waiting_resource"
  | "paused"
  | "failed"
  | "completed"
  | "cancelled"
  | "idle";

export const displayText: Record<DisplayStatus, string> = {
  queued: "等待开始",
  running: "进行中",
  attention: "需要你处理",
  waiting_resource: "等待资源",
  paused: "已暂停",
  failed: "执行遇到问题",
  completed: "已完成",
  cancelled: "已取消",
  idle: "对话中",
};

export interface PendingItem {
  /** approval:ID / card:状态:Goal / question:消息ID / unknown:任务；用于去重。 */
  id: string;
  kind: "approval" | "question" | "review" | "unknown";
  reason: string;
  /** 这一项代表的真实事项数（如同一状态下的卡片数）；缺省为 1。 */
  count?: number;
}

export interface TaskItem {
  key: string;
  sourceKind: SourceKind;
  sourceId: string;
  title: string;
  /** 最新业务进度，不是技术事件名。 */
  activity: string;
  status: DisplayStatus;
  updatedAt: string;
  pending: PendingItem[];
  /** 后端当前真实允许的操作；Goal 没有对应合同时为空。 */
  operations: TaskOperation[];
}

export type TaskFilter = "all" | "active" | "attention" | "completed";
export const filterText: Record<TaskFilter, string> = {
  all: "全部",
  active: "进行中",
  attention: "待处理",
  completed: "已完成",
};

const finished: DisplayStatus[] = ["completed", "failed", "cancelled"];
export const isFinished = (status: DisplayStatus) => finished.includes(status);

/** 只有已结束（或还没开始执行的空会话）的任务能删除；进行中的须先取消。 */
export const isRemovable = (item: Pick<TaskItem, "status">) =>
  isFinished(item.status) || item.status === "idle";

const statusMap: Record<Task["status"], DisplayStatus> = {
  PENDING: "queued",
  RUNNING: "running",
  WAITING_HUMAN: "attention",
  WAITING_BOT: "waiting_resource",
  PAUSED: "paused",
  COMPLETED: "completed",
  FAILED: "failed",
  CANCELLED: "cancelled",
};

const stamp = (value: string | undefined) => {
  if (!value) return 0;
  const parsed = Date.parse(value.endsWith("Z") ? value : value + "Z");
  return Number.isNaN(parsed) ? 0 : parsed;
};

function oneLine(text: string, max = 90) {
  const clean = text.replace(/\s+/g, " ").trim();
  return clean.length > max ? clean.slice(0, max - 1) + "…" : clean;
}

function taskActivity(task: Task, status: DisplayStatus) {
  const checkpoint = task.checkpoint ?? {};
  if (status === "completed") {
    const text = checkpoint.report?.summary || checkpoint.result;
    return text ? oneLine(text) : "任务已完成，可查看结果";
  }
  if (status === "failed") {
    const text = checkpoint.reason || checkpoint.detail;
    return text ? oneLine(String(text)) : "执行遇到问题，可查看原因";
  }
  const message = task.progress?.message?.trim();
  if (message) return oneLine(message);
  const step = task.plan?.steps?.find((s) => s.status !== "completed");
  if (status === "running" && step) return oneLine(step.description);
  return {
    queued: "已排队，等待开始",
    running: "正在执行",
    attention: "等待你处理",
    waiting_resource: "等待可用的助手或设备",
    paused: "已暂停",
    cancelled: "已取消",
    idle: "",
    completed: "",
    failed: "",
  }[status];
}

const attentionCards: Record<string, PendingItem["kind"]> = {
  WAITING_APPROVAL: "approval",
  BLOCKED: "question",
  REVIEW: "review",
};
const cardReason: Record<string, string> = {
  WAITING_APPROVAL: "张卡片等待批准执行",
  BLOCKED: "张卡片在等待回答",
  REVIEW: "张卡片等待验收",
};

export interface BuildInput {
  tasks: Task[];
  conversations: Conversation[];
  approvals: Approval[];
  goals: GoalSummary[];
  /** 审批查询失败时，WAITING_HUMAN 的原因无法确认。 */
  approvalsUnavailable?: boolean;
}

export function buildTaskItems(input: BuildInput): TaskItem[] {
  const { tasks, approvals, goals } = input;
  // 后端的 Goal 本身就是 settings.mode="goal" 的会话，也会出现在会话列表里。
  // 它们只以 Goal 条目展示，其会话和挂在其下的执行轮次都不再单独成项。
  const goalIds = new Set(goals.map((g) => g.id));
  for (const c of input.conversations)
    if (c.settings?.mode === "goal") goalIds.add(c.id);
  const conversations = input.conversations.filter((c) => !goalIds.has(c.id));
  const approvalsByTask = new Map<string, Approval[]>();
  for (const approval of approvals) {
    if (approval.status && approval.status !== "PENDING") continue;
    const list = approvalsByTask.get(approval.action.task_id) ?? [];
    list.push(approval);
    approvalsByTask.set(approval.action.task_id, list);
  }
  const tasksByConversation = new Map<string, Task[]>();
  const loose: Task[] = [];
  for (const task of tasks) {
    if (task.conversation_id && goalIds.has(task.conversation_id)) continue;
    if (!task.conversation_id) loose.push(task);
    else {
      const list = tasksByConversation.get(task.conversation_id) ?? [];
      list.push(task);
      tasksByConversation.set(task.conversation_id, list);
    }
  }

  function pendingFor(group: Task[]): PendingItem[] {
    const seen = new Map<string, PendingItem>();
    for (const task of group) {
      for (const approval of approvalsByTask.get(task.id) ?? [])
        seen.set(`approval:${approval.id}`, {
          id: `approval:${approval.id}`,
          kind: "approval",
          reason: "有一个动作等待你批准",
        });
    }
    for (const task of group) {
      const waiting = task.status === "WAITING_HUMAN";
      const known = (approvalsByTask.get(task.id) ?? []).length > 0;
      if (waiting && !known)
        seen.set(`unknown:${task.id}`, {
          id: `unknown:${task.id}`,
          kind: "unknown",
          reason: input.approvalsUnavailable
            ? "待处理详情暂时无法读取"
            : "等待原因暂不可用",
        });
    }
    return [...seen.values()];
  }

  function fromTasks(
    group: Task[],
    sourceKind: SourceKind,
    sourceId: string,
    conversation?: Conversation,
  ): TaskItem {
    const sorted = [...group].sort(
      (a, b) => stamp(b.updated_at) - stamp(a.updated_at),
    );
    const latest = sorted[0];
    const pending = pendingFor(group);
    let status = statusMap[latest.status];
    if (pending.length > 0) status = "attention";
    return {
      key: `${sourceKind}:${sourceId}`,
      sourceKind,
      sourceId,
      title: conversation?.title || latest.title,
      activity:
        status === "attention" && latest.status !== "WAITING_HUMAN"
          ? pending[0].reason
          : taskActivity(latest, status),
      status,
      updatedAt:
        stamp(conversation?.updated_at) > stamp(latest.updated_at)
          ? (conversation?.updated_at as string)
          : latest.updated_at,
      pending,
      operations: latest.progress?.allowed_operations ?? [],
    };
  }

  const items: TaskItem[] = [];
  const known = new Set(conversations.map((c) => c.id));
  for (const conversation of conversations) {
    const group = tasksByConversation.get(conversation.id);
    if (group?.length) {
      items.push(
        fromTasks(group, "conversation", conversation.id, conversation),
      );
    } else {
      items.push({
        key: `conversation:${conversation.id}`,
        sourceKind: "conversation",
        sourceId: conversation.id,
        title: conversation.title,
        activity: "还没有开始执行",
        status: "idle",
        updatedAt: conversation.updated_at ?? conversation.created_at,
        pending: [],
        operations: [],
      });
    }
  }
  // 会话列表缺失（分页或权限）时仍保留其执行轮次，不静默丢弃。
  for (const [id, group] of tasksByConversation)
    if (!known.has(id)) items.push(fromTasks(group, "conversation", id));
  for (const task of loose) items.push(fromTasks([task], "task", task.id));

  for (const goal of goals) {
    const counts = goal.cards ?? {};
    const total = Object.values(counts).reduce((a, b) => a + b, 0);
    const pending: PendingItem[] = [];
    if (goal.status !== "DONE")
      for (const [state, kind] of Object.entries(attentionCards))
        if ((counts[state] ?? 0) > 0)
          pending.push({
            id: `card:${state}:${goal.id}`,
            kind,
            reason: `${counts[state]}${cardReason[state]}`,
            count: counts[state],
          });
    // @kernel 用 ask_user 提出、用户尚未回复的问题不在任何卡片上，单独成项。
    if (goal.status !== "DONE" && goal.question)
      pending.push({
        id: `question:${goal.question.message_id}`,
        kind: "question",
        reason: `@kernel 在等你回答：${oneLine(goal.question.content, 40)}`,
      });
    const closed =
      (counts.DONE ?? 0) + (counts.FAILED ?? 0) + (counts.CANCELLED ?? 0);
    let status: DisplayStatus = "running";
    if (goal.status === "DONE") status = "completed";
    else if (pending.length) status = "attention";
    else if ((counts.RUNNING ?? 0) === 0 && (counts.READY ?? 0) > 0)
      status = "queued";
    const activity =
      status === "attention"
        ? pending.map((p) => p.reason).join("，")
        : total === 0
          ? "@kernel 正在规划步骤"
          : `已结束 ${closed}/${total} 张卡片${counts.RUNNING ? `，${counts.RUNNING} 张执行中` : ""}`;
    items.push({
      key: `goal:${goal.id}`,
      sourceKind: "goal",
      sourceId: goal.id,
      title: goal.title,
      activity,
      status,
      updatedAt: goal.updated_at,
      pending,
      operations: [],
    });
  }
  return sortItems(items);
}

/** 需要你处理的优先，其余按最新活动。 */
export function sortItems(items: TaskItem[]) {
  return [...items].sort((a, b) => {
    const left = a.pending.length > 0 ? 1 : 0;
    const right = b.pending.length > 0 ? 1 : 0;
    if (left !== right) return right - left;
    return stamp(b.updatedAt) - stamp(a.updatedAt);
  });
}

export function filterItems(
  items: TaskItem[],
  filter: TaskFilter,
  query = "",
): TaskItem[] {
  const needle = query.trim().toLowerCase();
  return items.filter((item) => {
    if (filter === "attention" && item.pending.length === 0) return false;
    if (filter === "completed" && item.status !== "completed") return false;
    if (
      filter === "active" &&
      (isFinished(item.status) || item.status === "idle")
    )
      return false;
    return (
      !needle || `${item.title} ${item.activity}`.toLowerCase().includes(needle)
    );
  });
}

/** 待处理数量 = 有未解决事项的任务数；事项数另算。两者都来自同一份列表。 */
export function pendingCounts(items: TaskItem[]) {
  const waiting = items.filter((item) => item.pending.length > 0);
  return {
    tasks: waiting.length,
    items: waiting.reduce(
      (sum, item) => sum + item.pending.reduce((n, p) => n + (p.count ?? 1), 0),
      0,
    ),
  };
}

/** 助手是否真的能接新工作：已启用、所在设备在线、引擎可用；读不到设备状态时记为“未知”。 */
export interface BotReadiness {
  usable: number;
  /** 已启用，但设备离线/停用或引擎不可用。 */
  blocked: number;
  /** 已启用，但设备状态读不到，无法断言。 */
  unknown: number;
}

export function botReadiness(
  bots: Bot[] | undefined,
  topology: Topology | undefined,
): BotReadiness {
  const result: BotReadiness = { usable: 0, blocked: 0, unknown: 0 };
  const nodes = new Map((topology?.nodes ?? []).map((n) => [n.id, n]));
  for (const bot of bots ?? []) {
    if (!bot.enabled) continue;
    const node = bot.node_id ? nodes.get(bot.node_id) : undefined;
    if (!bot.node_id || !node) {
      // 没读到设备信息（或找不到所在设备）：不能断言可用。
      result.unknown += 1;
    } else if (
      node.status !== "ONLINE" ||
      node.enabled === false ||
      node.engines?.[bot.engine]?.available === false
    ) {
      result.blocked += 1;
    } else {
      result.usable += 1;
    }
  }
  return result;
}

export interface RouteFacts {
  orchestrator?: boolean;
  legacy?: boolean;
  bots: BotReadiness;
}
export type NewTaskRoute =
  | { kind: "goal" }
  | { kind: "conversation" }
  | { kind: "draft"; reason: string };

/**
 * 新任务路由：编排已启用且至少有一个确认可用的助手时走 Goal；否则走仍可用的会话路径；
 * 两者都不可用则只保存草稿。提交后不会偷偷切换路径。
 */
export function chooseRoute(facts: RouteFacts): NewTaskRoute {
  if (facts.orchestrator === true && facts.bots.usable > 0)
    return { kind: "goal" };
  if (facts.legacy !== false) return { kind: "conversation" };
  let reason = "当前工作空间还没有完成运行配置。";
  if (facts.orchestrator === true)
    reason =
      facts.bots.blocked > 0 && facts.bots.unknown === 0
        ? "已启用的助手所在设备离线或引擎不可用，请先恢复设备。"
        : facts.bots.unknown > 0
          ? "暂时无法确认助手是否在线，状态待同步后再试。"
          : "没有可用的助手，请先在设置里启用助手。";
  return { kind: "draft", reason };
}
