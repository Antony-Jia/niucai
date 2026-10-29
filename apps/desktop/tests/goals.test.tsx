import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, expect, it, vi } from "vitest";
import { Goals } from "../src/pages/Goals";
import { request } from "../src/lib/api";

vi.mock("../src/lib/state", () => ({
  useApp: () => ({ connected: true, demo: false, notify: vi.fn() }),
  queryClient: new QueryClient(),
}));
vi.mock("../src/lib/api", () => ({ request: vi.fn(), setDemo: vi.fn() }));

const at = "2026-10-29T00:00:00";
const detail = {
  id: "g1",
  title: "修登录",
  status: "ACTIVE",
  orchestrator_enabled: true,
  messages: [
    {
      id: "m1",
      sequence: 1,
      role: "user",
      source: "USER",
      kind: "INSTRUCTION",
      content: "把登录页修好",
      created_at: at,
    },
    {
      id: "m2",
      sequence: 2,
      role: "assistant",
      source: "KERNEL",
      kind: "QUESTION",
      content: "用哪个环境？",
      created_at: at,
    },
  ],
  runs: [
    {
      id: "r1",
      status: "COMPLETED",
      triggers: [{ kind: "message" }],
      tool_calls: 2,
      output: "",
      error: null,
      created_at: at,
    },
  ],
  board: { cards: [], bots: [], statuses: [] },
};

function routes(path: string) {
  if (path === "/api/goals")
    return [
      {
        id: "g1",
        title: "修登录",
        status: "ACTIVE",
        cards: { READY: 2 },
        created_at: at,
        updated_at: at,
      },
    ];
  if (path === "/api/goals/g1") return detail;
  if (path === "/api/orchestrator/runs/r1")
    return {
      ...detail.runs[0],
      steps: [{ tool: "bots_list", args: {}, result: "[]" }],
    };
  return {};
}

beforeEach(() => {
  vi.mocked(request).mockReset();
  vi.mocked(request).mockImplementation(
    async (path: string) => routes(path) as never,
  );
});

it("shows a goal conversation and sends a reply to @kernel", async () => {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <Goals />
    </QueryClientProvider>,
  );
  expect(await screen.findByText("用哪个环境？")).toBeTruthy();
  expect(screen.getByText(/无可用bot/)).toBeTruthy();
  fireEvent.change(screen.getByLabelText("对 @kernel 说"), {
    target: { value: "用 staging" },
  });
  fireEvent.click(screen.getByText("发送"));
  await waitFor(() =>
    expect(request).toHaveBeenCalledWith(
      "/api/goals/g1/messages",
      "POST",
      expect.objectContaining({ content: "用 staging" }),
    ),
  );
  fireEvent.click(screen.getByText(/2 次工具调用/));
  expect(await screen.findByText("bots_list")).toBeTruthy();
});
