import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, expect, it, vi } from "vitest";
import { JudgePanel } from "../src/components/JudgePanel";
import { judgeText } from "../src/lib/judge";
import { request } from "../src/lib/api";

vi.mock("../src/lib/state", () => ({
  useApp: () => ({ connected: true, demo: false, notify: vi.fn() }),
  queryClient: new QueryClient(),
}));
vi.mock("../src/lib/api", () => ({ request: vi.fn(), setDemo: vi.fn() }));

const at = "2026-10-29T00:00:00";
const run = {
  id: "j1",
  card_id: "c1",
  card_title: "存储选型",
  conversation_id: "g1",
  question: "选什么数据库？",
  status: "DONE",
  verdict: "decided",
  cited_ids: ["m1"],
  record_ids: ["m1"],
  answer: "PostgreSQL",
  explanation: "同类问题",
  error: null,
  correction: null,
  correction_record_id: null,
  created_at: at,
  finished_at: at,
};

beforeEach(() => {
  vi.mocked(request).mockReset();
  vi.mocked(request).mockImplementation((async (path: string) => {
    if (path.startsWith("/api/judge/runs/j1/correct")) return { id: "m2" };
    return {
      counts: { decided: 1, returned: 2, held: 3 },
      pending: 0,
      enabled: true,
      runs: [run],
    };
  }) as never);
});

function panel() {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <JudgePanel goalId="g1" />
    </QueryClientProvider>,
  );
}

it("shows verdict counts and filters by verdict", async () => {
  panel();
  expect(await screen.findByText("代答 1")).toBeTruthy();
  expect(screen.getByText("退回 2")).toBeTruthy();
  expect(screen.getByText("转交用户 3")).toBeTruthy();
  expect(screen.getByText(/依据记录 m1/)).toBeTruthy();
  fireEvent.click(screen.getByText("退回 2"));
  await waitFor(() =>
    expect(request).toHaveBeenCalledWith(
      "/api/judge/runs?goal_id=g1&verdict=returned",
    ),
  );
});

it("submits a correction for a wrong verdict", async () => {
  panel();
  fireEvent.click(await screen.findByText("判错了"));
  fireEvent.change(screen.getByLabelText("纠正内容"), {
    target: { value: "用 MySQL" },
  });
  fireEvent.click(screen.getByText("提交纠正"));
  await waitFor(() =>
    expect(request).toHaveBeenCalledWith("/api/judge/runs/j1/correct", "POST", {
      correction: "用 MySQL",
    }),
  );
});

it("offers no correction for a run handed to the user", async () => {
  vi.mocked(request).mockImplementation((async () => ({
    counts: { decided: 0, returned: 0, held: 1 },
    pending: 0,
    enabled: true,
    runs: [{ ...run, verdict: "held", answer: "", cited_ids: [] }],
  })) as never);
  panel();
  expect(await screen.findByText(/· 转交用户/)).toBeTruthy();
  expect(screen.queryByText("判错了")).toBeNull();
});

it("describes the judge state on a card", () => {
  const base = { status: "BLOCKED" as const };
  expect(
    judgeText({
      ...base,
      judge: {
        id: "j",
        status: "PENDING",
        verdict: null,
        cited_ids: [],
        explanation: "",
      },
    }),
  ).toBe("裁判处理中");
  expect(
    judgeText({
      ...base,
      judge: {
        id: "j",
        status: "DONE",
        verdict: "decided",
        cited_ids: ["m1"],
        explanation: "",
      },
    }),
  ).toBe("裁判代答（依据记录 m1）");
  expect(
    judgeText({
      ...base,
      judge: {
        id: "j",
        status: "DONE",
        verdict: "returned",
        cited_ids: [],
        explanation: "",
      },
    }),
  ).toContain("已退回");
  expect(judgeText({ ...base, judge: null })).toBe("");
});
