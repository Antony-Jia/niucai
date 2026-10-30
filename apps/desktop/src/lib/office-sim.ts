import type { MapBot, NodeKind } from "./work-map";

/**
 * 办公室地图：布局与小人行为。纯函数/纯数据，不碰 React 和 DOM。
 * 小人的动作只是对 Bot 状态的表现（工作中坐工位，待命溜达，做完回白板登记），
 * 不会反过来改变任何任务状态，也不推断没有证据的事实。
 */
export interface Point {
  x: number;
  y: number;
}
export interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

export const HOUSE_W = 1280;
const MARGIN = 44;
const GUTTER_X = 22;
const CELL_W = 140;
const CELL_H = 100;
const ROOM_GAP = 24;
const ROOM_HEAD = 34;
const LOUNGE_BOTTOM = 420;
const ROOMS_TOP = 470;
const CORRIDOR = 46;
const COLS = 3;

export interface Desk {
  botId: string;
  cell: Rect;
  desk: Rect;
  /** 坐下时的位置（桌子后面）。 */
  seat: Point;
}
export interface Room {
  id: string;
  title: string;
  kind: NodeKind;
  nodeId: string;
  rect: Rect;
  row: number;
  doorX: number;
  /** 走廊中门外的位置；行号大于 0 的房间需要沿左侧过道回到休息室。 */
  corridorY: number;
  desks: Desk[];
}
export interface Spot {
  at: Point;
  say: string;
}
export interface Layout {
  width: number;
  height: number;
  board: Rect;
  shelf: Rect;
  /** 待命的小人在这个矩形里溜达。 */
  wander: Rect;
  spots: Spot[];
  rooms: Room[];
  deskOf: Map<string, { room: Room; desk: Desk }>;
}

const kindRank: Record<NodeKind, number> = { local: 0, remote: 1, unbound: 2 };

export function buildLayout(bots: MapBot[]): Layout {
  const groups = new Map<string, MapBot[]>();
  for (const bot of bots) {
    const list = groups.get(bot.nodeId) ?? [];
    list.push(bot);
    groups.set(bot.nodeId, list);
  }
  const titleOf = (list: MapBot[]) =>
    list[0].nodeName || (list[0].nodeId ? "未知设备" : "未绑定设备");
  const ordered = [...groups.entries()]
    .map(([nodeId, list]) => ({
      nodeId,
      title: titleOf(list),
      kind: list[0].nodeKind,
      bots: list
        .slice()
        .sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0)),
    }))
    .sort(
      (a, b) =>
        kindRank[a.kind] - kindRank[b.kind] ||
        (a.title < b.title ? -1 : a.title > b.title ? 1 : 0) ||
        (a.nodeId < b.nodeId ? -1 : 1),
    );

  const rooms: Room[] = [];
  let x = MARGIN;
  let y = ROOMS_TOP;
  let row = 0;
  let rowHeight = 0;
  for (const group of ordered) {
    const n = group.bots.length;
    const cols = Math.min(n, COLS);
    const rowsInRoom = Math.ceil(n / COLS);
    const w = Math.max(190, cols * CELL_W + 20);
    const h = ROOM_HEAD + rowsInRoom * CELL_H + 10;
    if (x > MARGIN && x + w > HOUSE_W - MARGIN) {
      y += rowHeight + CORRIDOR;
      row += 1;
      x = MARGIN;
      rowHeight = 0;
    }
    const rect = { x, y, w, h };
    const left = x + (w - cols * CELL_W) / 2;
    const id = group.nodeId || "unbound";
    rooms.push({
      id,
      title: group.title,
      kind: group.kind,
      nodeId: group.nodeId,
      rect,
      row,
      doorX: x + w / 2,
      corridorY: row === 0 ? ROOMS_TOP - 24 : y - CORRIDOR / 2,
      desks: group.bots.map((bot, i) => {
        const cell = {
          x: left + (i % COLS) * CELL_W,
          y: y + ROOM_HEAD + Math.floor(i / COLS) * CELL_H,
          w: CELL_W,
          h: CELL_H,
        };
        return {
          botId: bot.id,
          cell,
          desk: { x: cell.x + 22, y: cell.y + 30, w: 96, h: 36 },
          seat: { x: cell.x + 70, y: cell.y + 44 },
        };
      }),
    });
    x += w + ROOM_GAP;
    rowHeight = Math.max(rowHeight, h);
  }

  const deskOf = new Map<string, { room: Room; desk: Desk }>();
  for (const room of rooms)
    for (const desk of room.desks) deskOf.set(desk.botId, { room, desk });

  return {
    width: HOUSE_W,
    height: rooms.length ? y + rowHeight + 44 : ROOMS_TOP,
    board: { x: HOUSE_W / 2 - 230, y: 18, w: 460, h: 132 },
    shelf: { x: HOUSE_W - 330, y: 18, w: 250, h: 110 },
    wander: { x: 200, y: 196, w: HOUSE_W - 400, h: 214 },
    spots: [
      { at: { x: 178, y: 270 }, say: "歇一会儿" },
      { at: { x: HOUSE_W - 190, y: 262 }, say: "倒杯咖啡" },
      { at: { x: 178, y: 392 }, say: "看看绿植" },
    ],
    rooms,
    deskOf,
  };
}

/** 从座位走出房间的路线（带所处区域），反过来就是回座位的路线。 */
function outRoute(room: Room, desk: Desk): (Point & { where: string })[] {
  const inRoom = `room:${room.id}`;
  const aisleX = desk.cell.x + 11;
  const topY = room.rect.y + 28;
  const route: (Point & { where: string })[] = [
    { x: aisleX, y: desk.seat.y, where: inRoom },
    { x: aisleX, y: topY, where: inRoom },
    { x: room.doorX, y: topY, where: inRoom },
    { x: room.doorX, y: room.corridorY, where: "lounge" },
  ];
  if (room.row > 0) {
    route.push(
      { x: GUTTER_X, y: room.corridorY, where: "lounge" },
      { x: GUTTER_X, y: LOUNGE_BOTTOM, where: "lounge" },
    );
  }
  return route;
}

export type Mode = "working" | "idle" | "away" | "freeze";
export const modeOf = (availability: MapBot["availability"]): Mode =>
  availability === "working"
    ? "working"
    : availability === "idle"
      ? "idle"
      : availability === "unavailable"
        ? "away"
        : "freeze";

interface Stop extends Point {
  dwell: number;
  where: string;
  say?: string;
  sit?: boolean;
}
export interface Actor {
  id: string;
  x: number;
  y: number;
  facing: 1 | -1;
  where: string;
  mode: Mode;
  sitting: boolean;
  moving: boolean;
  say: string | null;
  stops: Stop[];
  wait: number;
}

export type Rng = () => number;
/** 稳定的伪随机数：测试可复现，也让不同 Bot 的节奏错开。 */
export function seededRng(seed: string): Rng {
  let h = 1779033703 ^ seed.length;
  for (let i = 0; i < seed.length; i++) {
    h = Math.imul(h ^ seed.charCodeAt(i), 3432918353);
    h = (h << 13) | (h >>> 19);
  }
  let a = h >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const randomIn = (rect: Rect, rng: Rng): Point => ({
  x: rect.x + rng() * rect.w,
  y: rect.y + rng() * rect.h,
});

/** 白板前的站位；每个 Bot 站位略有错开，同时登记时不会叠在一起。 */
export function boardStand(layout: Layout, id: string): Point {
  const rng = seededRng(`board:${id}`);
  return {
    x:
      layout.board.x + 40 + Math.floor(rng() * 9) * ((layout.board.w - 80) / 8),
    y: layout.board.y + layout.board.h + 30,
  };
}

const seatStop = (entry: { room: Room; desk: Desk }): Stop => ({
  ...entry.desk.seat,
  dwell: 0,
  where: `room:${entry.room.id}`,
  sit: true,
});

export function spawn(
  bot: MapBot,
  layout: Layout,
  rng: Rng,
  anchor?: Point,
): Actor {
  const mode = modeOf(bot.availability);
  const entry = layout.deskOf.get(bot.id);
  const atDesk = mode !== "idle" && entry;
  const start = atDesk
    ? entry.desk.seat
    : (anchor ?? randomIn(layout.wander, rng));
  return {
    id: bot.id,
    x: start.x,
    y: start.y,
    facing: rng() < 0.5 ? 1 : -1,
    where: atDesk ? `room:${entry.room.id}` : "lounge",
    mode,
    sitting: !!atDesk,
    moving: false,
    say: null,
    stops: [],
    // 错开第一次出发的时间。
    wait: mode === "idle" ? rng() * 2500 : 0,
  };
}

export interface Errand {
  /** 回白板登记做完的事。 */
  register?: string;
  /** 去白板领取卡片。 */
  claim?: string;
}

/**
 * Bot 状态变化时重新规划：
 * 工作中 → 去白板领取（有的话）再回工位坐下；待命 → 先去白板登记再溜达；
 * 不可用 → 回工位待着；状态未知 → 原地不动，不假装知道发生了什么。
 */
export function setMode(
  actor: Actor,
  layout: Layout,
  mode: Mode,
  errand: Errand = {},
) {
  const entry = layout.deskOf.get(actor.id);
  actor.mode = mode;
  actor.wait = 0;
  if (mode === "freeze") {
    actor.stops = [];
    actor.moving = false;
    return;
  }
  const stand = boardStand(layout, actor.id);
  const errands: Stop[] = [];
  if (errand.register)
    errands.push({
      ...stand,
      dwell: 2800,
      where: "lounge",
      say: errand.register,
    });
  if (errand.claim)
    errands.push({ ...stand, dwell: 1400, where: "lounge", say: errand.claim });

  const inRoom = actor.where.startsWith("room:");
  const leave: Stop[] =
    inRoom && entry
      ? outRoute(entry.room, entry.desk).map((p) => ({ ...p, dwell: 0 }))
      : [];
  const enter: Stop[] = entry
    ? [
        ...outRoute(entry.room, entry.desk)
          .reverse()
          .map((p) => ({ ...p, dwell: 0 })),
        seatStop(entry),
      ]
    : [];

  if (mode === "idle") {
    // 先走出房间，再去白板登记；没有登记任务就直接溜达。
    actor.stops = [...leave, ...errands];
  } else if (inRoom && entry) {
    // 已经在自己的房间里：坐着就不动，除非要先去白板办事。
    actor.stops =
      mode === "working" && errands.length
        ? [...leave, ...errands, ...enter]
        : actor.sitting
          ? []
          : [seatStop(entry)];
  } else {
    actor.stops = [...(mode === "working" ? errands : []), ...enter];
  }
}

function planWander(actor: Actor, layout: Layout, rng: Rng) {
  const spot =
    rng() < 0.28
      ? layout.spots[Math.floor(rng() * layout.spots.length)]
      : undefined;
  const at = spot ? spot.at : randomIn(layout.wander, rng);
  actor.stops = [
    {
      ...at,
      dwell: spot ? 2500 + rng() * 2500 : 1200 + rng() * 4500,
      where: "lounge",
      say: spot?.say,
    },
  ];
}

export const WALK_SPEED = 96;

/** 推进 dt 毫秒。待命的小人没事做时自己找下一个溜达的位置。 */
export function tick(actor: Actor, layout: Layout, dt: number, rng: Rng) {
  if (actor.wait > 0) {
    actor.wait -= dt;
    if (actor.wait > 0) return;
    actor.say = null;
    actor.wait = 0;
  }
  if (!actor.stops.length) {
    actor.moving = false;
    if (actor.mode === "idle" && !actor.where.startsWith("room:"))
      planWander(actor, layout, rng);
    return;
  }
  const next = actor.stops[0];
  actor.sitting = false;
  const dx = next.x - actor.x;
  const dy = next.y - actor.y;
  const dist = Math.hypot(dx, dy);
  const step = (WALK_SPEED * dt) / 1000;
  if (Math.abs(dx) > 0.5) actor.facing = dx > 0 ? 1 : -1;
  if (dist <= step) {
    actor.x = next.x;
    actor.y = next.y;
    actor.where = next.where;
    actor.stops.shift();
    actor.wait = next.dwell;
    actor.say = next.say ?? null;
    actor.sitting = !!next.sit;
    actor.moving = false;
    // 到达白板站位时面向白板。
    if (next.say)
      actor.facing = actor.x < layout.board.x + layout.board.w / 2 ? 1 : -1;
  } else {
    actor.x += (dx / dist) * step;
    actor.y += (dy / dist) * step;
    actor.moving = true;
  }
}
