import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import {
  createEventTracker,
  excerpt,
  pickBubbles,
  projectWorkMap,
  type MapEvent,
} from "../src/lib/work-map";
import type { Bot, Card } from "../src/lib/board";
import type { Topology } from "../src/components/NodesPanel";
import { App } from "../src/App";
import { Provider } from "../src/lib/state";

vi.mock("@tauri-apps/api/core", () => ({
  isTauri: () => false,
  invoke: vi.fn(),
}));

const bot = (id: string, over: Partial<Bot> = {}): Bot =>
  ({
    id,
    name: `bot-${id}`,
    enabled: true,
    running: 0,
    node_id: "n1",
    role: "",
    engine: "pi",
    ...over,
  }) as Bot;
const card = (id: string, over: Partial<Card> = {}): Card =>
  ({
    id,
    title: `卡片 ${id}`,
    status: "READY",
    assignee_bot_id: null,
    current_job: null,
    latest: null,
    summary: "",
    ...over,
  }) as Card;
const topology = (status: "ONLINE" | "OFFLINE" | "DISABLED"): Topology =>
  ({
    kernel: { instance_id: "k", remote_enabled: true },
    nodes: [{ id: "n1", name: "节点一", status }],
  }) as unknown as Topology;

describe("work map projection", () => {
  it("assigns seats by sorted bot id and keeps them when status changes", () => {
    const bots = ["d", "b", "a", "c"].map((id) => bot(id));
    const before = projectWorkMap({ bots, cards: [] });
    const after = projectWorkMap({
      bots,
      cards: [card("1", { status: "RUNNING", assignee_bot_id: "c" })],
    });
    const seat = (data: typeof before) =>
      Object.fromEntries(data.bots.map((b) => [b.id, b.seat]));
    expect(seat(before)).toEqual({ a: 0, b: 1, c: 2, d: 3 });
    expect(seat(after)).toEqual(seat(before));
  });

  it("limits each zone to eight bots", () => {
    const bots = Array.from({ length: 11 }, (_, i) =>
      bot(String(i).padStart(2, "0")),
    );
    const data = projectWorkMap({ bots, cards: [] });
    expect(data.zones).toBe(2);
    expect(data.bots.filter((b) => b.zone === 0)).toHaveLength(8);
    expect(data.bots[8]).toMatchObject({ zone: 1, seat: 0 });
  });

  it("reports availability honestly: offline, stale, concurrent work", () => {
    const bots = [bot("a"), bot("b", { enabled: false })];
    const running = [
      card("1", { status: "RUNNING", assignee_bot_id: "a" }),
      card("2", { status: "RUNNING", assignee_bot_id: "a" }),
    ];
    const online = projectWorkMap({
      bots,
      cards: running,
      topology: topology("ONLINE"),
    });
    expect(online.bots[0]).toMatchObject({
      availability: "working",
      runningCount: 2,
    });
    expect(online.bots[1].availability).toBe("unavailable");
    expect(
      projectWorkMap({ bots, cards: running, topology: topology("OFFLINE") })
        .bots[0].availability,
    ).toBe("unavailable");
    expect(
      projectWorkMap({ bots, cards: running, stale: true }).bots[0]
        .availability,
    ).toBe("unknown");
  });

  it("hides closed cards unless asked and only delivers real results", () => {
    const cards = [
      card("1", { status: "DONE", summary: "完成了" }),
      card("2", { status: "DONE" }),
      card("3", { status: "READY" }),
    ];
    expect(projectWorkMap({ bots: [], cards }).groups.closed).toHaveLength(0);
    const all = projectWorkMap({ bots: [], cards, includeClosed: true });
    expect(all.groups.closed).toHaveLength(2);
    expect(all.delivered.map((c) => c.id)).toEqual(["1"]);
  });

  it("counts bots that took a card from the public pool via current_job", () => {
    const pooled = card("1", {
      status: "RUNNING",
      assignee_bot_id: null,
      current_job: { bot_id: "b" },
    } as Partial<Card>);
    const data = projectWorkMap({
      bots: [bot("a"), bot("b")],
      cards: [pooled],
      topology: topology("ONLINE"),
      onlyAssigned: true,
    });
    expect(data.bots.map((b) => b.id)).toEqual(["b"]);
    expect(data.bots[0]).toMatchObject({
      availability: "working",
      runningCount: 1,
    });
  });

  it("keeps a pooled card attributed to its bot after it blocks or enters review", () => {
    const job = { id: "j1", status: "COMPLETED", bot_id: "a", engine: "pi" };
    const cards = [
      card("1", { status: "BLOCKED", last_job: job }),
      card("2", { status: "REVIEW", last_job: job }),
      // 重新排队的卡片不再归属于上一次的执行者。
      card("3", { status: "READY", last_job: job }),
    ];
    const bots = [bot("a"), bot("b")];
    const all = projectWorkMap({ bots, cards, topology: topology("ONLINE") });
    expect(all.bots[0].blocked.map((c) => c.id)).toEqual(["1"]);
    expect(all.bots[0].review.map((c) => c.id)).toEqual(["2"]);
    expect(all.bots[1].blocked).toHaveLength(0);
    const scoped = projectWorkMap({
      bots,
      cards,
      topology: topology("ONLINE"),
      onlyAssigned: true,
    });
    expect(scoped.bots.map((b) => b.id)).toEqual(["a"]);
  });

  it("lists comments and @mentions by backend event id", () => {
    const withComment = card("1", {
      status: "RUNNING",
      latest: { kind: "progress", body: "跑到一半", at: "2026-01-01T00:00:05" },
      recent_events: [
        {
          id: 11,
          kind: "comment",
          actor: "coder",
          body: "@reviewer 请看接口",
          mentions: ["reviewer"],
          at: "2026-01-01T00:00:09",
        },
        {
          id: 10,
          kind: "progress",
          actor: "coder",
          body: "跑到一半",
          mentions: [],
          at: "2026-01-01T00:00:05",
        },
      ],
    });
    const data = projectWorkMap({ bots: [], cards: [withComment] });
    expect(data.events.map((e) => e.key)).toEqual(["1#11", "1#10"]);
    expect(data.events[0]).toMatchObject({
      kind: "comment",
      mentions: ["reviewer"],
    });
    // 同一事件重复到达（相同事件 ID）只算一次。
    const again = projectWorkMap({
      bots: [],
      cards: [withComment, withComment],
    });
    expect(again.events).toHaveLength(2);
    const tracker = createEventTracker();
    tracker.fresh(data.events);
    expect(tracker.fresh(data.events)).toEqual([]);
  });

  it("reports unknown instead of working or idle when device info is missing", () => {
    const cards = [card("1", { status: "RUNNING", assignee_bot_id: "a" })];
    const bots = [bot("a"), bot("b"), bot("c", { enabled: false })];
    for (const input of [
      { topology: null },
      { topology: topology("ONLINE"), topologyUnknown: true },
    ]) {
      const data = projectWorkMap({ bots, cards, ...input });
      expect(data.bots.map((b) => b.availability)).toEqual([
        "unknown",
        "unknown",
        "unavailable",
      ]);
    }
  });

  it("scopes to assigned bots for a single goal", () => {
    const data = projectWorkMap({
      bots: [bot("a"), bot("b")],
      cards: [card("1", { assignee_bot_id: "b" })],
      onlyAssigned: true,
    });
    expect(data.bots.map((b) => b.id)).toEqual(["b"]);
  });
});

describe("event playback", () => {
  const events = (ids: string[]): MapEvent[] =>
    ids.map((id) => ({
      key: id,
      cardId: id,
      cardTitle: id,
      kind: "progress",
      body: "x",
      at: "2026-01-01T00:00:00Z",
    }));
  it("primes on the first snapshot, then plays only unseen events once", () => {
    const tracker = createEventTracker();
    expect(tracker.fresh(events(["a", "b"]))).toEqual([]);
    expect(tracker.fresh(events(["a", "b"]))).toEqual([]);
    expect(tracker.fresh(events(["a", "b", "c"])).map((e) => e.key)).toEqual([
      "c",
    ]);
    expect(tracker.fresh(events(["c", "a"]))).toEqual([]);
    tracker.reset();
    expect(tracker.fresh(events(["z"]))).toEqual([]);
  });

  it("keeps at most two bubbles and puts questions first", () => {
    const list = events(["a", "b", "c"]);
    list[2].kind = "question";
    const picked = pickBubbles(list, 2);
    expect(picked).toHaveLength(2);
    expect(picked[0].kind).toBe("question");
  });

  it("truncates long excerpts", () => {
    expect(excerpt("a".repeat(100), 10)).toEqual({
      text: "aaaaaaaaa…",
      cut: true,
    });
  });
});

describe("work map in the app", () => {
  it("switches between list and map, labels demo data and hides content", async () => {
    localStorage.clear();
    render(
      <Provider>
        <App />
      </Provider>,
    );
    fireEvent.click(await screen.findByRole("button", { name: "体验示例" }));
    await screen.findByText("示例模式 · 不连接真实模型或设备");
    fireEvent.click(await screen.findByRole("button", { name: "地图" }));
    expect(
      await screen.findByText("示例数据 · 非真实助手"),
    ).toBeInTheDocument();
    const board = await screen.findByRole("button", { name: /共享白板/ });
    // 首次快照只建立基线，不会把已有动态当成新事件播放。
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(document.querySelector(".bubbles")).toBeNull();
    const hide = screen.getByRole("button", { name: /隐藏工作内容/ });
    fireEvent.click(hide);
    expect(hide).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(board);
    expect(
      await screen.findByRole("heading", { name: "白板" }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "列表" }));
    await waitFor(() =>
      expect(
        screen.getByRole("group", { name: "任务筛选" }),
      ).toBeInTheDocument(),
    );
  });
});
