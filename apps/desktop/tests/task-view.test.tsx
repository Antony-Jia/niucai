import { describe, expect, it } from "vitest";
import {
  botReadiness,
  buildTaskItems,
  chooseRoute,
  filterItems,
  pendingCounts,
} from "../src/lib/task-view";
import type { Approval, Conversation, Task } from "../src/lib/types";
import type { GoalSummary } from "../src/lib/goals";
import type { Bot } from "../src/lib/board";
import type { Topology } from "../src/components/NodesPanel";

const task = (over: Partial<Task>): Task =>
  ({
    id: "t1",
    conversation_id: null,
    title: "任务",
    goal: "",
    status: "RUNNING",
    computer_id: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    current_step: 0,
    retry_count: 0,
    plan: {},
    checkpoint: {},
    ...over,
  }) as Task;
const conversation = (id: string): Conversation => ({
  id,
  title: `会话 ${id}`,
  created_at: "2026-01-01T00:00:00Z",
});
const approval = (id: string, taskId: string): Approval =>
  ({
    id,
    status: "PENDING",
    action: { task_id: taskId },
  }) as unknown as Approval;
const goal = (over: Partial<GoalSummary>): GoalSummary =>
  ({
    id: "g1",
    title: "目标",
    status: "ACTIVE",
    cards: {},
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-02T00:00:00Z",
    ...over,
  }) as GoalSummary;

describe("task view adapter", () => {
  it("keeps sourceKind + sourceId so equal ids never merge", () => {
    const items = buildTaskItems({
      tasks: [task({ id: "x", conversation_id: null })],
      conversations: [conversation("x")],
      approvals: [],
      goals: [goal({ id: "g" })],
    });
    expect(items.map((i) => i.key).sort()).toEqual([
      "conversation:x",
      "goal:g",
      "task:x",
    ]);
  });

  it("shows a goal once even though the backend also lists it as a conversation", () => {
    const asGoal = {
      ...conversation("g1"),
      settings: { mode: "goal", autonomy: { actions: false, remote: false } },
    } as unknown as Conversation;
    const items = buildTaskItems({
      // 编排卡片的执行轮次可能挂在 Goal 会话下，也不应再冒出一条“会话”。
      tasks: [task({ id: "card-task", conversation_id: "g1" })],
      conversations: [asGoal, conversation("c2")],
      approvals: [],
      goals: [goal({ id: "g1" })],
    });
    expect(items.map((i) => i.key).sort()).toEqual([
      "conversation:c2",
      "goal:g1",
    ]);
    // 即使 Goal 列表没读到，也按会话类型排除，而不是当作普通聊天。
    const fromConversationsOnly = buildTaskItems({
      tasks: [],
      conversations: [asGoal],
      approvals: [],
      goals: [],
    });
    expect(fromConversationsOnly).toHaveLength(0);
  });

  it("counts a task once even with several approvals, and reports items separately", () => {
    const items = buildTaskItems({
      tasks: [
        task({ id: "a", conversation_id: "c1", status: "WAITING_HUMAN" }),
      ],
      conversations: [conversation("c1")],
      approvals: [approval("p1", "a"), approval("p2", "a")],
      goals: [],
    });
    expect(pendingCounts(items)).toEqual({ tasks: 1, items: 2 });
    expect(items[0].status).toBe("attention");
  });

  it("flags WAITING_HUMAN without a readable approval instead of hiding it", () => {
    const items = buildTaskItems({
      tasks: [task({ id: "a", status: "WAITING_HUMAN" })],
      conversations: [],
      approvals: [],
      goals: [],
      approvalsUnavailable: true,
    });
    expect(items[0].pending[0]).toMatchObject({ kind: "unknown" });
    expect(pendingCounts(items).tasks).toBe(1);
  });

  it("derives goal attention from blocked/review cards and puts it first", () => {
    const items = buildTaskItems({
      tasks: [task({ id: "a", status: "RUNNING" })],
      conversations: [],
      approvals: [],
      goals: [goal({ cards: { BLOCKED: 1, REVIEW: 2, RUNNING: 1 } })],
    });
    expect(items[0].sourceKind).toBe("goal");
    expect(items[0].pending).toHaveLength(2);
    expect(filterItems(items, "attention")).toHaveLength(1);
    expect(filterItems(items, "active")).toHaveLength(2);
  });

  it("does not ask for attention on finished goals", () => {
    const items = buildTaskItems({
      tasks: [],
      conversations: [],
      approvals: [],
      goals: [goal({ status: "DONE", cards: { REVIEW: 1, DONE: 3 } })],
    });
    expect(pendingCounts(items).tasks).toBe(0);
    expect(filterItems(items, "completed")).toHaveLength(1);
  });

  it("counts real cards, not card states, in the pending item total", () => {
    const items = buildTaskItems({
      tasks: [],
      conversations: [],
      approvals: [],
      goals: [goal({ cards: { BLOCKED: 1, REVIEW: 2 } })],
    });
    expect(items[0].pending).toHaveLength(2);
    expect(pendingCounts(items)).toEqual({ tasks: 1, items: 3 });
  });

  it("surfaces an unanswered @kernel question as a pending item", () => {
    const items = buildTaskItems({
      tasks: [],
      conversations: [],
      approvals: [],
      goals: [
        goal({
          cards: { RUNNING: 1 },
          question: {
            message_id: "m9",
            content: "要发布到哪个环境？",
            created_at: "2026-01-02T00:00:00Z",
          },
        }),
      ],
    });
    expect(items[0].status).toBe("attention");
    expect(items[0].pending).toEqual([
      expect.objectContaining({ id: "question:m9", kind: "question" }),
    ]);
    expect(items[0].activity).toContain("要发布到哪个环境");
    expect(pendingCounts(items)).toEqual({ tasks: 1, items: 1 });
    const done = buildTaskItems({
      tasks: [],
      conversations: [],
      approvals: [],
      goals: [
        goal({
          status: "DONE",
          question: { message_id: "m9", content: "x", created_at: "" },
        }),
      ],
    });
    expect(done[0].pending).toHaveLength(0);
  });

  it("only routes to a Goal when a bot is confirmed usable", () => {
    const ready = { usable: 1, blocked: 0, unknown: 0 };
    const none = { usable: 0, blocked: 0, unknown: 0 };
    expect(
      chooseRoute({ orchestrator: true, legacy: true, bots: ready }),
    ).toEqual({ kind: "goal" });
    expect(
      chooseRoute({ orchestrator: true, legacy: true, bots: none }),
    ).toEqual({ kind: "conversation" });
    // 只有“已启用但离线”或“状态未知”的助手，不算可用。
    expect(
      chooseRoute({
        orchestrator: true,
        legacy: true,
        bots: { usable: 0, blocked: 2, unknown: 0 },
      }),
    ).toEqual({ kind: "conversation" });
    const offline = chooseRoute({
      orchestrator: true,
      legacy: false,
      bots: { usable: 0, blocked: 2, unknown: 0 },
    });
    expect(offline).toEqual({
      kind: "draft",
      reason: expect.stringContaining("离线"),
    });
    const unknown = chooseRoute({
      orchestrator: true,
      legacy: false,
      bots: { usable: 0, blocked: 0, unknown: 1 },
    });
    expect(unknown).toEqual({
      kind: "draft",
      reason: expect.stringContaining("待同步"),
    });
    expect(
      chooseRoute({ orchestrator: false, legacy: undefined, bots: ready }),
    ).toEqual({ kind: "conversation" });
    expect(
      chooseRoute({ orchestrator: true, legacy: false, bots: none }).kind,
    ).toBe("draft");
  });

  it("derives bot readiness from node status and engine availability", () => {
    const bot = (id: string, node_id: string, enabled = true) =>
      ({ id, node_id, enabled, engine: "pi" }) as unknown as Bot;
    const node = (id: string, status: string, available = true) => ({
      id,
      status,
      enabled: status !== "DISABLED",
      engines: { pi: { available } },
    });
    const topology = {
      kernel: { instance_id: "k", remote_enabled: true },
      nodes: [
        node("on", "ONLINE"),
        node("off", "OFFLINE"),
        node("noeng", "ONLINE", false),
      ],
    } as unknown as Topology;
    expect(
      botReadiness(
        [
          bot("a", "on"),
          bot("b", "off"),
          bot("c", "noeng"),
          bot("d", "gone"),
          bot("e", "on", false),
        ],
        topology,
      ),
    ).toEqual({ usable: 1, blocked: 2, unknown: 1 });
    expect(botReadiness([bot("a", "on")], undefined)).toEqual({
      usable: 0,
      blocked: 0,
      unknown: 1,
    });
  });
});
