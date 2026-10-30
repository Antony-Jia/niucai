import { describe, expect, it } from "vitest";
import {
  boardStand,
  buildLayout,
  modeOf,
  seededRng,
  setMode,
  spawn,
  tick,
  type Actor,
  type Layout,
} from "../src/lib/office-sim";
import type { MapBot, NodeKind } from "../src/lib/work-map";

const bot = (
  id: string,
  over: Partial<MapBot> & { kind?: NodeKind } = {},
): MapBot => {
  const { kind = "local", ...rest } = over;
  return {
    id,
    name: id,
    shortId: id,
    role: "",
    engine: "pi",
    nodeName: kind === "remote" ? "VPS" : "本机",
    availability: "idle",
    reason: "",
    running: [],
    blocked: [],
    review: [],
    runningCount: 0,
    zone: 0,
    seat: 0,
    nodeId: kind === "remote" ? "n-remote" : "n-local",
    nodeKind: kind,
    model: "",
    reasoningEffort: "",
    concurrency: 1,
    skills: [],
    mcpServers: [],
    tags: [],
    finished: [],
    ...rest,
  };
};

/** 推进直到停下或超时，返回花掉的毫秒数。 */
function settle(actor: Actor, layout: Layout, limit = 120_000) {
  const rng = seededRng(`t:${actor.id}`);
  let spent = 0;
  while (spent < limit) {
    tick(actor, layout, 100, rng);
    spent += 100;
    if (!actor.stops.length && actor.wait <= 0 && !actor.moving) break;
  }
  return spent;
}

describe("buildLayout", () => {
  it("每台设备一间房，本机在前、远端其后、未绑定最后", () => {
    const layout = buildLayout([
      bot("r1", { kind: "remote" }),
      bot("u1", { kind: "unbound", nodeId: "", nodeName: "" }),
      bot("l1"),
    ]);
    expect(layout.rooms.map((r) => r.kind)).toEqual([
      "local",
      "remote",
      "unbound",
    ]);
  });

  it("每个 Bot 恰有一张工位，且落在自己的房间内", () => {
    const bots = ["a", "b", "c", "d", "e"].map((id) => bot(id));
    const layout = buildLayout(bots);
    for (const b of bots) {
      const entry = layout.deskOf.get(b.id);
      expect(entry).toBeDefined();
      const { room, desk } = entry!;
      expect(desk.seat.x).toBeGreaterThan(room.rect.x);
      expect(desk.seat.x).toBeLessThan(room.rect.x + room.rect.w);
      expect(desk.seat.y).toBeGreaterThan(room.rect.y);
      expect(desk.seat.y).toBeLessThan(room.rect.y + room.rect.h);
    }
    expect(layout.deskOf.size).toBe(5);
  });

  it("房间过多时换行，且不超出房子宽度", () => {
    const bots = Array.from({ length: 6 }, (_, i) =>
      bot(`b${i}`, { nodeId: `n${i}`, nodeName: `设备${i}` }),
    );
    const layout = buildLayout(bots);
    expect(Math.max(...layout.rooms.map((r) => r.row))).toBeGreaterThan(0);
    for (const room of layout.rooms)
      expect(room.rect.x + room.rect.w).toBeLessThanOrEqual(layout.width);
  });

  it("没有 Bot 时没有房间也不报错", () => {
    const layout = buildLayout([]);
    expect(layout.rooms).toHaveLength(0);
    expect(layout.height).toBeGreaterThan(0);
  });
});

describe("modeOf", () => {
  it("把可用性映射成行为", () => {
    expect(modeOf("working")).toBe("working");
    expect(modeOf("idle")).toBe("idle");
    expect(modeOf("unavailable")).toBe("away");
    expect(modeOf("unknown")).toBe("freeze");
  });
});

describe("actor", () => {
  it("工作中的 Bot 一开始就坐在工位上", () => {
    const b = bot("a", { availability: "working" });
    const layout = buildLayout([b]);
    const actor = spawn(b, layout, seededRng("a"));
    const seat = layout.deskOf.get("a")!.desk.seat;
    expect([actor.x, actor.y]).toEqual([seat.x, seat.y]);
    expect(actor.sitting).toBe(true);
  });

  it("待命的 Bot 出生在休息区，并会自己溜达", () => {
    const b = bot("a");
    const layout = buildLayout([b]);
    const rng = seededRng("a");
    const actor = spawn(b, layout, rng);
    expect(actor.sitting).toBe(false);
    expect(actor.where).toBe("lounge");
    const start = { x: actor.x, y: actor.y };
    for (let i = 0; i < 600; i++) tick(actor, layout, 100, rng);
    expect(Math.hypot(actor.x - start.x, actor.y - start.y)).toBeGreaterThan(0);
    // 溜达只发生在休息区内：随机游走区域，或沙发/咖啡机/绿植旁的固定去处。
    const points = [
      { x: layout.wander.x, y: layout.wander.y },
      {
        x: layout.wander.x + layout.wander.w,
        y: layout.wander.y + layout.wander.h,
      },
      ...layout.spots.map((s) => s.at),
    ];
    const xs = points.map((p) => p.x);
    const ys = points.map((p) => p.y);
    expect(actor.x).toBeGreaterThanOrEqual(Math.min(...xs) - 1);
    expect(actor.x).toBeLessThanOrEqual(Math.max(...xs) + 1);
    expect(actor.y).toBeGreaterThanOrEqual(Math.min(...ys) - 1);
    expect(actor.y).toBeLessThanOrEqual(Math.max(...ys) + 1);
    expect(actor.y).toBeLessThan(layout.rooms[0].rect.y);
  });

  it("待命转工作：走回工位坐下", () => {
    const b = bot("a");
    const layout = buildLayout([b]);
    const actor = spawn(b, layout, seededRng("a"));
    setMode(actor, layout, "working");
    settle(actor, layout);
    const seat = layout.deskOf.get("a")!.desk.seat;
    expect([actor.x, actor.y]).toEqual([seat.x, seat.y]);
    expect(actor.sitting).toBe(true);
    expect(actor.where).toBe(`room:${layout.deskOf.get("a")!.room.id}`);
  });

  it("工作转待命：先走出房间，到白板登记，再溜达", () => {
    const b = bot("a", { availability: "working" });
    const layout = buildLayout([b]);
    const actor = spawn(b, layout, seededRng("a"));
    setMode(actor, layout, "idle", { register: "登记：写报告 已完成" });
    const stand = boardStand(layout, "a");
    const rng = seededRng("a");
    let said = "";
    let atBoard = false;
    for (let i = 0; i < 2000 && !atBoard; i++) {
      tick(actor, layout, 100, rng);
      if (actor.say) said = actor.say;
      atBoard = actor.x === stand.x && actor.y === stand.y;
    }
    expect(atBoard).toBe(true);
    expect(said).toBe("登记：写报告 已完成");
    expect(actor.where).toBe("lounge");
    expect(actor.sitting).toBe(false);
  });

  it("需要登记领取时，去白板后再回工位", () => {
    const b = bot("a");
    const layout = buildLayout([b]);
    const actor = spawn(b, layout, seededRng("a"));
    setMode(actor, layout, "working", { claim: "领取：写报告" });
    const rng = seededRng("a");
    const seen: string[] = [];
    for (let i = 0; i < 2000; i++) {
      tick(actor, layout, 100, rng);
      if (actor.say && !seen.includes(actor.say)) seen.push(actor.say);
    }
    expect(seen).toContain("领取：写报告");
    expect(actor.sitting).toBe(true);
  });

  it("行走路线不穿墙：第二排房间的 Bot 沿左侧过道出来", () => {
    const bots = Array.from({ length: 6 }, (_, i) =>
      bot(`b${i}`, {
        nodeId: `n${i}`,
        nodeName: `设备${i}`,
        availability: "working",
      }),
    );
    const layout = buildLayout(bots);
    const lower = layout.rooms.find((r) => r.row > 0)!;
    const id = lower.desks[0].botId;
    const actor = spawn(
      bots.find((b) => b.id === id)!,
      layout,
      seededRng(id),
    );
    setMode(actor, layout, "idle");
    const rng = seededRng(id);
    for (let i = 0; i < 800 && actor.where !== "lounge"; i++)
      tick(actor, layout, 100, rng);
    expect(actor.where).toBe("lounge");
  });

  it("不可用的 Bot 回到工位待着，不再外出", () => {
    const b = bot("a");
    const layout = buildLayout([b]);
    const actor = spawn(b, layout, seededRng("a"));
    setMode(actor, layout, "away");
    settle(actor, layout);
    const seat = layout.deskOf.get("a")!.desk.seat;
    expect([actor.x, actor.y]).toEqual([seat.x, seat.y]);
    const rng = seededRng("a");
    for (let i = 0; i < 300; i++) tick(actor, layout, 100, rng);
    expect([actor.x, actor.y]).toEqual([seat.x, seat.y]);
  });

  it("状态未知时原地不动，不假装知道发生了什么", () => {
    const b = bot("a");
    const layout = buildLayout([b]);
    const rng = seededRng("a");
    const actor = spawn(b, layout, rng);
    for (let i = 0; i < 30; i++) tick(actor, layout, 100, rng);
    setMode(actor, layout, "freeze");
    const at = { x: actor.x, y: actor.y };
    for (let i = 0; i < 300; i++) tick(actor, layout, 100, rng);
    expect({ x: actor.x, y: actor.y }).toEqual(at);
    expect(actor.moving).toBe(false);
  });

  it("没有工位的 Bot 转工作时不会崩溃", () => {
    const b = bot("a");
    const layout = buildLayout([b]);
    const ghost = spawn(bot("ghost"), layout, seededRng("g"));
    expect(() => setMode(ghost, layout, "working")).not.toThrow();
    expect(() => settle(ghost, layout)).not.toThrow();
  });
});

describe("seededRng", () => {
  it("相同种子给出相同序列，不同种子不同", () => {
    const a = seededRng("x");
    const b = seededRng("x");
    const c = seededRng("y");
    const seq = (r: () => number) => [r(), r(), r()];
    expect(seq(a)).toEqual(seq(b));
    expect(seq(seededRng("x"))).not.toEqual(seq(c));
  });
});
