import { request } from "./api";
import type { JobEvent } from "./job-log";

export type Engine = "pi" | "codex" | "claude" | "hermes";
export type PermissionProfile = "read" | "workspace" | "full";
export type CardStatus =
  | "DRAFT"
  | "READY"
  | "WAITING_APPROVAL"
  | "RUNNING"
  | "WAITING"
  | "REVIEW"
  | "BLOCKED"
  | "DONE"
  | "FAILED"
  | "CANCELLED";

export interface BotConfig {
  name: string;
  node_id: string;
  engine: Engine;
  role: string;
  instructions: string;
  tags: string[];
  model: string;
  /** 空表示用引擎默认；可选值由节点心跳上报。 */
  reasoning_effort?: string;
  permission_profile: PermissionProfile;
  skills: string[];
  mcp_servers: string[];
  requires_approval: boolean;
  concurrency: number;
}
/** 手填的环境说明：只写名称和说明，不要写密码、令牌等凭据。 */
export interface BotEnvironmentManual {
  logged_in_sites: string[];
  resources: string[];
  notes: string;
}
/** Runner 探测并上报的环境事实，只读。 */
export interface BotEnvironmentProbed {
  os?: string;
  engine?: { name: string; available?: boolean; version?: string };
  browser?: { available: boolean; cdp: boolean };
  repos?: { name: string; remote: string }[];
  cli?: string[];
}
export interface BotEnvironment {
  probed: BotEnvironmentProbed;
  manual: Partial<BotEnvironmentManual>;
}

/** Bot 之间的委派策略；名单条目是 Bot 名称、tag:<标签> 或 *。 */
export interface CollabPolicy {
  can_delegate: boolean;
  delegate_to: string[];
  can_ask: boolean;
  can_discuss: boolean;
  can_verify: boolean;
  accept_from: string[];
  auto_approve_from: string[];
}
/** 与 Kernel 默认值一致；C2 只强制执行 can_delegate / delegate_to / accept_from / auto_approve_from，其余留给 C4/C5。 */
export const defaultCollabPolicy: CollabPolicy = {
  can_delegate: false,
  delegate_to: [],
  can_ask: true,
  can_discuss: false,
  can_verify: true,
  accept_from: ["*"],
  auto_approve_from: [],
};

export interface Bot extends BotConfig {
  id: string;
  /** 同事视角的环境画像；探测部分由节点上报，手填部分可在 Bot 设置里编辑。 */
  environment?: BotEnvironment;
  collab_policy?: CollabPolicy;
  enabled: boolean;
  running: number;
  node_name?: string;
  config_version: number;
  /** 绑定了但不存在或已停用的资产，形如 skill:x / mcp:y。 */
  missing_assets?: string[];
  /** 绑定的 MCP 需要在节点上设置的环境变量名。 */
  node_env?: string[];
}
export interface Card {
  id: string;
  title: string;
  goal: string;
  acceptance: string;
  status: CardStatus;
  assignee_bot_id: string | null;
  assignee_name: string | null;
  required_tags: string[];
  depends_on: string[];
  priority: number;
  review: "none" | "llm";
  attempts: number;
  version: number;
  budget: { max_attempts?: number; max_minutes?: number | null };
  current_job: {
    id: string;
    status: string;
    bot_id: string;
    engine: string;
  } | null;
  /** 最近一次 Attempt：BLOCKED / REVIEW 时 current_job 已清空，仍可据此知道执行者。 */
  last_job?: {
    id: string;
    status: string;
    bot_id: string | null;
    engine: string;
  } | null;
  latest: { kind: string; body: string; at: string } | null;
  /** 最近几条卡片动态（含评论与 @提及），带稳定的事件 ID。 */
  recent_events?: {
    id: number;
    kind: string;
    actor: string;
    body: string;
    mentions: string[];
    at: string;
  }[];
  /** 委派出的卡片：发起委派的 Bot 名称；用户或编排创建的卡片没有。 */
  initiator_name?: string | null;
  /** task 为普通任务；ask 为交谈卡（Bot 之间的提问，或用户与 Bot 的私聊线程）。 */
  kind?: "task" | "ask";
  /** 委派 / 提问时指向发起方的卡片。 */
  parent_card_id?: string | null;
  created_by?: string;
  /** 最近一次裁判（C3）：处理中 / 代答 / 退回 / 转交用户；没有裁判参与时为空。 */
  judge?: {
    id: string;
    status: "PENDING" | "DONE" | "FAILED";
    verdict: "decided" | "returned" | "held" | null;
    cited_ids: string[];
    explanation: string;
  } | null;
  /** WAITING 时正在等待的卡片与条件。 */
  waiting_on?: WaitingOn | null;
  summary: string;
  created_at: string;
  updated_at: string;
}
export interface WaitingOn {
  cards: string[];
  mode: "all" | "any";
  until?: string;
  timed_out?: boolean;
}
export interface CardEvent {
  id: number;
  seq: number;
  kind: string;
  actor: string;
  body: string;
  mentions: string[];
  data: Record<string, unknown>;
  created_at: string;
}
export interface Attempt {
  id: string;
  bot_id: string | null;
  engine: string;
  status: string;
  node_id: string | null;
  created_at: string;
  updated_at: string;
  error?: string | null;
}
export interface CardDetail extends Card {
  result: { summary?: string; files?: { path: string }[] } & Record<
    string,
    unknown
  >;
  events: CardEvent[];
  /** 每次领取对应一个远端作业（Attempt）。 */
  runs: Attempt[];
}
export interface Board {
  cards: Card[];
  bots: { id: string; name: string; engine: Engine; enabled: boolean }[];
  statuses: CardStatus[];
}
export interface NewCard {
  title: string;
  goal: string;
  acceptance: string;
  assignee_bot_id: string | null;
  required_tags: string[];
  depends_on: string[];
  priority: number;
  review: "none" | "llm";
  draft: boolean;
}
export type CardOperation =
  "ready" | "approve" | "cancel" | "accept" | "reject" | "answer" | "reopen";

export const statusText: Record<CardStatus, string> = {
  DRAFT: "草稿",
  READY: "待领取",
  WAITING_APPROVAL: "待批准",
  RUNNING: "执行中",
  WAITING: "等待其它卡片",
  REVIEW: "待验收",
  BLOCKED: "等待回答",
  DONE: "已完成",
  FAILED: "失败",
  CANCELLED: "已取消",
};
/** 白板列：已结束的三种状态合并为一列。 */
export const boardColumns: {
  key: string;
  label: string;
  statuses: CardStatus[];
}[] = [
  { key: "draft", label: "草稿", statuses: ["DRAFT"] },
  { key: "ready", label: "待领取", statuses: ["READY", "WAITING_APPROVAL"] },
  { key: "running", label: "执行中", statuses: ["RUNNING", "WAITING"] },
  { key: "attention", label: "需要你", statuses: ["BLOCKED", "REVIEW"] },
  { key: "closed", label: "已结束", statuses: ["DONE", "FAILED", "CANCELLED"] },
];

/** 当前状态下可执行的操作；需要正文的操作在详情里单独给输入框。 */
export function cardOperations(status: CardStatus): CardOperation[] {
  const closed = ["DONE", "FAILED", "CANCELLED"].includes(status);
  const ops: CardOperation[] = [];
  if (status === "DRAFT") ops.push("ready");
  if (status === "WAITING_APPROVAL") ops.push("approve");
  if (status === "BLOCKED") ops.push("answer");
  if (status === "REVIEW") ops.push("accept", "reject");
  if (closed) ops.push("reopen");
  else ops.push("cancel");
  return ops;
}
export const operationText: Record<CardOperation, string> = {
  ready: "发布",
  approve: "批准执行",
  cancel: "取消",
  accept: "验收通过",
  reject: "退回修改",
  answer: "回答并继续",
  reopen: "重开",
};
export const needsBody = (op: CardOperation) =>
  op === "answer" || op === "reject";

/** WAITING 卡片在卡片上的一行说明：等哪几张、全部还是任一。 */
export function waitingText(card: Card, all: Card[] = []): string {
  const waiting = card.waiting_on;
  if (card.status !== "WAITING" || !waiting) return "";
  const names = waiting.cards.map(
    (id) => all.find((c) => c.id === id)?.title ?? id.slice(0, 8),
  );
  const how = waiting.cards.length > 1 && waiting.mode === "any" ? "任一" : "";
  return `等待${how ? "（" + how + "）" : ""}：${names.join("、")}`;
}

/** 委派来源的一行说明；非委派卡片返回空串。 */
export const initiatorText = (card: Card) =>
  card.initiator_name ? `由 @${card.initiator_name} 委派` : "";

export const splitList = (value: string) =>
  value
    .split(/[,，\s]+/)
    .map((s) => s.trim())
    .filter(Boolean);

export const boardApi = {
  board: () => request<Board>("/api/board"),
  bots: () => request<Bot[]>("/api/bots"),
  createBot: (bot: BotConfig & { collab_policy?: Partial<CollabPolicy> }) =>
    request<Bot>("/api/bots", "POST", bot),
  updateBot: (
    id: string,
    changes: Partial<BotConfig> & {
      enabled?: boolean;
      environment?: BotEnvironmentManual;
      collab_policy?: Partial<CollabPolicy>;
    },
  ) => request<Bot>(`/api/bots/${id}`, "PATCH", changes),
  deleteBot: (id: string) => request(`/api/bots/${id}`, "DELETE"),
  card: (id: string) => request<CardDetail>(`/api/cards/${id}`),
  createCard: (card: NewCard) => request<Card>("/api/cards", "POST", card),
  action: (id: string, op: CardOperation, version: number, body = "") =>
    request<Card>(`/api/cards/${id}/${op}`, "POST", {
      expected_version: version,
      body,
    }),
  comment: (id: string, body: string) =>
    request(`/api/cards/${id}/comments`, "POST", {
      body,
      idempotency_key: crypto.randomUUID(),
    }),
  chats: (botId: string) => request<Card[]>(`/api/bots/${botId}/chats`),
  createChat: (botId: string, message: string, allowUnsafe = false) =>
    request<Card>(`/api/bots/${botId}/chats`, "POST", {
      message,
      allow_unsafe_profile: allowUnsafe,
    }),
  sendChat: (cardId: string, message: string) =>
    request<Card>(`/api/cards/${cardId}/messages`, "POST", { message }),
  jobEvents: (id: string, after = 0) =>
    request<JobEvent[]>(
      `/api/remote/jobs/${id}/events${after ? `?after=${after}` : ""}`,
    ),
};

/** Kernel 每页最多返回 200 条：按游标取到最后一页。 */
export async function allJobEvents(id: string, after = 0) {
  const events: JobEvent[] = [];
  for (let page = 0; page < 50; page += 1) {
    const batch = await boardApi.jobEvents(id, events.at(-1)?.id ?? after);
    events.push(...batch);
    if (batch.length < 200) break;
  }
  return events;
}
