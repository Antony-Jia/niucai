import { request } from "./api";
import type { Board } from "./board";

export type GoalStatus = "ACTIVE" | "DONE";
export interface GoalSummary {
  id: string;
  title: string;
  status: GoalStatus;
  /** 本 Goal 下各状态的卡片数。 */
  cards: Record<string, number>;
  /** @kernel 用 ask_user 提出、用户尚未回复的问题。 */
  question?: { message_id: string; content: string; created_at: string } | null;
  updated_at: string;
  created_at: string;
}
export interface GoalMessage {
  id: string;
  sequence: number;
  role: "user" | "assistant";
  source: string;
  /** INSTRUCTION（用户）、REPLY / QUESTION / FINAL（编排）。 */
  kind: string;
  content: string;
  created_at: string;
}
export interface OrchestratorRun {
  id: string;
  conversation_id: string;
  status: "PENDING" | "RUNNING" | "COMPLETED" | "FAILED";
  triggers: { kind: string; title?: string; body?: string }[];
  output: string;
  usage: Record<string, number>;
  error: string | null;
  tool_calls?: number;
  steps?: { tool: string; args: Record<string, unknown>; result: string }[];
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}
export interface GoalDetail {
  id: string;
  title: string;
  status: GoalStatus;
  orchestrator_enabled: boolean;
  question?: GoalSummary["question"];
  messages: GoalMessage[];
  runs: OrchestratorRun[];
  board: Board;
}

export const triggerText: Record<string, string> = {
  message: "新消息",
  manual: "手动",
  review: "待验收",
  blocked: "Bot 提问",
  proposal: "Bot 提议",
  mention: "@kernel",
  done: "卡片完成",
  failed: "卡片失败",
  cancelled: "卡片取消",
  all_closed: "全部结束",
};

export const goalsApi = {
  list: () => request<GoalSummary[]>("/api/goals"),
  get: (id: string) => request<GoalDetail>(`/api/goals/${id}`),
  /** clientId 是幂等键：响应丢失后用同一个 clientId 重试，Kernel 返回已创建的 Goal。 */
  create: (
    message: string,
    title = "",
    clientId?: string,
    /** 用户用 @ 指定的助手名称；编排者优先交给他们。 */
    bots: string[] = [],
  ) =>
    request<{ id: string }>("/api/goals", "POST", {
      message,
      title,
      ...(clientId ? { client_id: clientId } : {}),
      ...(bots.length ? { bots } : {}),
    }),
  remove: (id: string) => request(`/api/goals/${id}`, "DELETE"),
  send: (id: string, content: string) =>
    request(`/api/goals/${id}/messages`, "POST", {
      content,
      client_id: crypto.randomUUID(),
    }),
  rerun: (id: string) => request(`/api/goals/${id}/run`, "POST"),
  run: (id: string) => request<OrchestratorRun>(`/api/orchestrator/runs/${id}`),
};
