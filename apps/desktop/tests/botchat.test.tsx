import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, expect, it, vi } from "vitest";
import { BotChat } from "../src/components/BotChat";
import { BoardColumns, askText } from "../src/pages/Whiteboard";
import { request } from "../src/lib/api";
import type { Bot, Card } from "../src/lib/board";

const state = vi.hoisted(() => ({
  connected: true,
  demo: false,
  notify: vi.fn(),
}));
vi.mock("../src/lib/state", () => ({
  useApp: () => state,
  queryClient: new QueryClient(),
}));
vi.mock("../src/lib/api", () => ({ request: vi.fn(), setDemo: vi.fn() }));

const bot = { id: "b1", name: "reviewer" } as unknown as Bot;
const thread = {
  id: "c1",
  title: "这个函数做什么",
  status: "DONE",
  kind: "ask",
  events: [
    {
      id: 1,
      seq: 1,
      kind: "comment",
      actor: "user",
      body: "这个函数做什么",
      created_at: "2026-01-01T00:00:00Z",
    },
    {
      id: 2,
      seq: 2,
      kind: "reply",
      actor: "reviewer",
      body: "它负责解析配置",
      created_at: "2026-01-01T00:00:05Z",
    },
  ],
  runs: [],
};

function wrap(node: React.ReactNode) {
  return render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      {node}
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.mocked(request)
    .mockReset()
    .mockImplementation(async (path, method) => {
      if (path === "/api/bots/b1/chats" && method === "POST")
        return thread as never;
      if (path === "/api/bots/b1/chats") return [thread] as never;
      if (path === "/api/cards/c1/messages") return thread as never;
      if (path === "/api/cards/c1") return thread as never;
      return [] as never;
    });
});

it("lists threads and shows the conversation with replies", async () => {
  wrap(<BotChat bot={bot} onClose={() => {}} />);
  fireEvent.click(await screen.findByText(/这个函数做什么 ·/));
  expect(await screen.findByText("它负责解析配置")).toBeTruthy();
  fireEvent.change(screen.getByLabelText("私聊消息"), {
    target: { value: "再解释一下" },
  });
  fireEvent.click(screen.getByText("发送"));
  await waitFor(() =>
    expect(request).toHaveBeenCalledWith("/api/cards/c1/messages", "POST", {
      message: "再解释一下",
      allow_unsafe_profile: undefined,
    }),
  );
});

it("starts a new chat", async () => {
  wrap(<BotChat bot={bot} onClose={() => {}} />);
  fireEvent.change(await screen.findByLabelText("新对话"), {
    target: { value: "你好" },
  });
  fireEvent.click(screen.getByText("发送"));
  await waitFor(() =>
    expect(request).toHaveBeenCalledWith("/api/bots/b1/chats", "POST", {
      message: "你好",
      allow_unsafe_profile: false,
    }),
  );
});

it("folds ask cards under their parent and labels who asked whom", () => {
  const base = {
    goal: "",
    acceptance: "",
    required_tags: [],
    depends_on: [],
    attempts: 0,
    budget: {},
    current_job: null,
    latest: null,
    version: 1,
    priority: 0,
    review: "none",
    assignee_bot_id: null,
  };
  const parent = {
    ...base,
    id: "p",
    title: "父卡",
    status: "RUNNING",
    assignee_name: "A",
  } as unknown as Card;
  const ask = {
    ...base,
    id: "q",
    title: "问个事实",
    status: "RUNNING",
    kind: "ask",
    parent_card_id: "p",
    initiator_name: "A",
    assignee_name: "B",
  } as unknown as Card;
  expect(askText(ask)).toBe("@A 问 @B");
  wrap(<BoardColumns cards={[parent, ask]} onOpen={() => {}} />);
  expect(screen.queryByText("问个事实")).toBeNull();
  expect(screen.getByText(/@A 问 @B/)).toBeTruthy();
});

it("shows only your messages and named replies, plus why a thread failed", async () => {
  vi.mocked(request).mockImplementation(async (path) => {
    if (path === "/api/cards/c1")
      return {
        ...thread,
        status: "FAILED",
        assignee_name: "reviewer",
        result: { error: "节点不再提供只读档位" },
        events: [
          ...thread.events,
          {
            id: 3,
            seq: 3,
            kind: "comment",
            actor: "bot:x",
            body: "别的 Bot 插进来的话",
            created_at: "2026-01-01T00:00:06Z",
          },
        ],
      } as never;
    return [thread] as never;
  });
  wrap(<BotChat bot={bot} onClose={() => {}} />);
  fireEvent.click(await screen.findByText(/这个函数做什么 ·/));
  expect(await screen.findByText("它负责解析配置")).toBeTruthy();
  expect(screen.queryByText("别的 Bot 插进来的话")).toBeNull();
  expect(screen.getByText(/@reviewer ·/)).toBeTruthy();
  expect(screen.getByRole("status").textContent).toContain(
    "节点不再提供只读档位",
  );
});

it("labels questions from the orchestrator", () => {
  expect(
    askText({
      kind: "ask",
      created_by: "kernel",
      initiator_name: null,
      assignee_name: "B",
    } as unknown as Card),
  ).toBe("@kernel 问 @B");
});
