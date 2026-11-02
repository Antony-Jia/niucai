import { request } from "./api";
import type { Card } from "./board";

export type Verdict = "decided" | "returned" | "held";
export interface JudgeRun {
  id: string;
  card_id: string;
  card_title: string;
  conversation_id: string | null;
  question: string;
  status: "PENDING" | "DONE" | "FAILED" | "DISCARDED";
  verdict: Verdict | null;
  /** 裁判引用的 decision 记录；record_ids 是提供给模型的全部记录。 */
  cited_ids: string[];
  record_ids: string[];
  answer: string;
  explanation: string;
  error: string | null;
  correction: string | null;
  correction_record_id: string | null;
  created_at: string;
  finished_at: string | null;
}
export interface JudgeRuns {
  counts: Record<Verdict, number>;
  pending: number;
  enabled: boolean;
  runs: JudgeRun[];
}
export interface DecisionRecord {
  id: string;
  scope: "global" | "goal" | "bot";
  goal_id: string | null;
  question: string;
  answer: string;
  principle: boolean;
  corrects: string | null;
  corrected_by: string | null;
}

export const verdictText: Record<Verdict, string> = {
  decided: "代答",
  returned: "退回",
  held: "转交用户",
};

/** 卡片上的一行裁判状态；没有裁判参与时返回空串。 */
export function judgeText(card: Pick<Card, "status" | "judge">): string {
  const judge = card.judge;
  if (!judge) return "";
  if (judge.status === "PENDING") return "裁判处理中";
  if (judge.verdict === "decided")
    return `裁判代答（依据记录 ${judge.cited_ids.join("、")}）`;
  if (judge.verdict === "returned") return "已退回：裁判认为提问需要修改";
  if (judge.verdict === "held") return "裁判已转交给你";
  return "";
}

export const judgeApi = {
  runs: (goalId?: string | null, verdict?: Verdict | "") => {
    const query = new URLSearchParams();
    if (goalId) query.set("goal_id", goalId);
    if (verdict) query.set("verdict", verdict);
    const suffix = query.size ? `?${query}` : "";
    return request<JudgeRuns>(`/api/judge/runs${suffix}`);
  },
  correct: (runId: string, correction: string) =>
    request<DecisionRecord>(`/api/judge/runs/${runId}/correct`, "POST", {
      correction,
    }),
  decisions: (goalId?: string | null) =>
    request<{ decisions: DecisionRecord[] }>(
      `/api/judge/decisions${goalId ? `?goal_id=${goalId}` : ""}`,
    ),
  setPrinciple: (id: string, principle: boolean) =>
    request<DecisionRecord>(`/api/judge/decisions/${id}`, "PATCH", {
      principle,
    }),
};
