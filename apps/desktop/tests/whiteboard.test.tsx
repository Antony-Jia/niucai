import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, expect, it, vi } from "vitest";
import { Whiteboard } from "../src/pages/Whiteboard";
import { cardOperations } from "../src/lib/board";
import { request } from "../src/lib/api";

vi.mock("../src/lib/state", () => ({
  useApp: () => ({ connected: true, demo: false, notify: vi.fn() }),
  queryClient: new QueryClient(),
}));
vi.mock("../src/lib/api", () => ({ request: vi.fn(), setDemo: vi.fn() }));

const card = {
  id: "c1",
  title: "Fix login",
  goal: "登录页报错",
  acceptance: "能登录",
  status: "BLOCKED",
  assignee_bot_id: "b1",
  assignee_name: "coder",
  required_tags: [],
  depends_on: [],
  priority: 0,
  review: "none",
  attempts: 1,
  version: 4,
  budget: { max_attempts: 3 },
  current_job: null,
  latest: { kind: "question", body: "用哪个账号测试？", at: "" },
  summary: "",
  created_at: "2026-10-29T00:00:00",
  updated_at: "2026-10-29T00:00:00",
};
const board = {
  cards: [
    card,
    { ...card, id: "c2", title: "Docs", status: "DONE", latest: null },
  ],
  bots: [{ id: "b1", name: "coder", engine: "claude", enabled: true }],
  statuses: [],
};
const detail = {
  ...card,
  result: {},
  events: [
    {
      id: 1,
      seq: 1,
      kind: "question",
      actor: "bot:coder",
      body: "用哪个账号测试？",
      mentions: [],
      data: {},
      created_at: "2026-10-29T00:00:00",
    },
  ],
  runs: [
    {
      id: "j1",
      bot_id: "b1",
      engine: "claude",
      status: "COMPLETED",
      node_id: "n1",
      created_at: "2026-10-29T00:00:00",
      updated_at: "2026-10-29T00:00:00",
    },
  ],
};

function routes(path: string) {
  if (path === "/api/board") return board;
  if (path === "/api/cards/c1") return detail;
  if (path === "/api/remote/jobs/j1/events")
    return [
      {
        id: 9,
        type: "remote.job.progress",
        created_at: "2026-10-29T00:00:00",
        data: {
          kind: "tool_execution_start",
          payload: { tool: "Read", input: "src/app.py", engine: "claude" },
        },
      },
      {
        id: 10,
        type: "remote.job.progress",
        created_at: "2026-10-29T00:00:02",
        data: {
          kind: "tool_execution_end",
          payload: {
            tool: "Read",
            is_error: false,
            output: "print(1)",
            engine: "claude",
          },
        },
      },
    ];
  return {};
}

function mount() {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <Whiteboard />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.mocked(request).mockReset();
  vi.mocked(request).mockImplementation(
    async (path: string) => routes(path) as never,
  );
});

it("groups cards into columns and answers a blocked card", async () => {
  mount();
  expect(await screen.findByText("Fix login")).toBeTruthy();
  expect(screen.getByText("需要你").parentElement?.textContent).toContain("1");
  expect(screen.getByText("用哪个账号测试？")).toBeTruthy();
  fireEvent.click(screen.getByText("Fix login"));
  const answer = await screen.findByLabelText("回答或退回意见");
  const submit = screen.getByText("回答并继续") as HTMLButtonElement;
  expect(submit.disabled).toBe(true);
  fireEvent.change(answer, { target: { value: "用 test 账号" } });
  fireEvent.click(submit);
  await waitFor(() =>
    expect(request).toHaveBeenCalledWith("/api/cards/c1/answer", "POST", {
      expected_version: 4,
      body: "用 test 账号",
    }),
  );
  fireEvent.click(screen.getByText(/claude · COMPLETED/));
  expect(await screen.findByText("Read")).toBeTruthy();
  expect(screen.getByText(/成功 · 2 秒/)).toBeTruthy();
  expect(screen.getByText(/1 次工具调用/)).toBeTruthy();
  expect(screen.getByText("src/app.py")).toBeTruthy();
  expect(screen.getByText("print(1)")).toBeTruthy();
});

it("creates a card for a bot", async () => {
  mount();
  await screen.findByText("Fix login");
  fireEvent.click(screen.getByText("新建卡片"));
  fireEvent.change(screen.getByLabelText("标题"), {
    target: { value: "New card" },
  });
  fireEvent.change(screen.getByLabelText("负责 Bot"), {
    target: { value: "b1" },
  });
  fireEvent.click(screen.getByText("发布"));
  await waitFor(() =>
    expect(request).toHaveBeenCalledWith(
      "/api/cards",
      "POST",
      expect.objectContaining({
        title: "New card",
        assignee_bot_id: "b1",
        draft: false,
        review: "none",
      }),
    ),
  );
});

it("says there is no bot available", async () => {
  vi.mocked(request).mockImplementation(
    async () => ({ cards: [], bots: [], statuses: [] }) as never,
  );
  mount();
  expect(await screen.findByText(/无可用bot/)).toBeTruthy();
});

it("offers the operations each status allows", () => {
  expect(cardOperations("DRAFT")).toEqual(["ready", "cancel"]);
  expect(cardOperations("REVIEW")).toEqual(["accept", "reject", "cancel"]);
  expect(cardOperations("FAILED")).toEqual(["reopen"]);
  expect(cardOperations("RUNNING")).toEqual(["cancel"]);
});
