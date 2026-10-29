import { request } from "./api";

export type Engine = "pi" | "codex" | "claude";
export type PermissionProfile = "read" | "workspace" | "full";
export type CardStatus =
  | "DRAFT"
  | "READY"
  | "WAITING_APPROVAL"
  | "RUNNING"
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
  permission_profile: PermissionProfile;
  skills: string[];
  mcp_servers: string[];
  requires_approval: boolean;
  concurrency: number;
}
export interface Bot extends BotConfig {
  id: string;
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
  latest: { kind: string; body: string; at: string } | null;
  summary: string;
  created_at: string;
  updated_at: string;
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
  { key: "running", label: "执行中", statuses: ["RUNNING"] },
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

export const splitList = (value: string) =>
  value
    .split(/[,，\s]+/)
    .map((s) => s.trim())
    .filter(Boolean);

export const boardApi = {
  board: () => request<Board>("/api/board"),
  bots: () => request<Bot[]>("/api/bots"),
  createBot: (bot: BotConfig) => request<Bot>("/api/bots", "POST", bot),
  updateBot: (
    id: string,
    changes: Partial<BotConfig> & { enabled?: boolean },
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
  jobEvents: (id: string) =>
    request<
      {
        id: number;
        type: string;
        created_at: string;
        data: { kind?: string; payload?: Record<string, unknown> };
      }[]
    >(`/api/remote/jobs/${id}/events`),
};
