import { useEffect, useMemo, useRef, useState } from "react";
import type { Card } from "../lib/board";
import {
  buildLayout,
  modeOf,
  seededRng,
  setMode,
  spawn,
  tick,
  type Actor,
  type Errand,
  type Layout,
  type Rect,
  type Rng,
} from "../lib/office-sim";
import {
  boardGroups,
  excerpt,
  type MapBot,
  type MapEvent,
  type NodeKind,
  type WorkMapData,
} from "../lib/work-map";
import type { Topology } from "./NodesPanel";

export const palette = [
  "#5b7fc0",
  "#c2755a",
  "#6aa384",
  "#a78bc7",
  "#c9a24d",
  "#6fa3b8",
];
export function colorFor(id: string) {
  let hash = 0;
  for (const ch of id) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
  return palette[hash % palette.length];
}

export const noteColor: Record<string, string> = {
  READY: "#e8c872",
  WAITING_APPROVAL: "#e8a872",
  RUNNING: "#7fb2e8",
  BLOCKED: "#e88c8c",
  REVIEW: "#b79be0",
  DONE: "#8fcf9f",
  FAILED: "#c76d6d",
  CANCELLED: "#b5b9c2",
  DRAFT: "#dcdfe5",
};

/** 8×8 像素小人；颜色由 Bot ID 稳定决定，只是外观，不代表引擎或状态。 */
export function Sprite({ color, size = 40 }: { color: string; size?: number }) {
  const rows = [
    "..hhhh..",
    ".hhhhhh.",
    ".ffffff.",
    ".fefefe.",
    "..ffff..",
    ".bbbbbb.",
    "bbbbbbbb",
    ".bb..bb.",
  ];
  const fill = { h: "#3b3a46", f: "#f2cfa8", e: "#2b2b33", b: color } as Record<
    string,
    string
  >;
  return (
    <svg
      className="sprite"
      viewBox="0 0 8 8"
      width={size}
      height={size}
      shapeRendering="crispEdges"
      aria-hidden="true"
    >
      {rows.flatMap((row, y) =>
        [...row].map((c, x) =>
          c === "." ? null : (
            <rect
              key={`${x}-${y}`}
              x={x}
              y={y}
              width="1"
              height="1"
              fill={fill[c]}
            />
          ),
        ),
      )}
    </svg>
  );
}

const outcomeText: Record<string, string> = {
  DONE: "已完成",
  REVIEW: "提交验收",
  BLOCKED: "需要回答",
  FAILED: "未完成",
  CANCELLED: "已取消",
  READY: "退回白板",
  WAITING_APPROVAL: "退回白板",
};

/** 回白板登记的说明：只用卡片当前的真实状态，找不到卡片就只说“已结束”。 */
function registerText(
  finished: string[],
  cards: Card[],
  show: (text: string) => string,
) {
  if (!finished.length) return "回白板登记";
  const byId = new Map(cards.map((c) => [c.id, c]));
  const first = byId.get(finished[0]);
  const more = finished.length > 1 ? ` 等 ${finished.length} 项` : "";
  if (!first) return `登记：已结束${more}`;
  return `登记：${show(excerpt(first.title, 12).text)} ${outcomeText[first.status] ?? "已更新"}${more}`;
}

const kindText: Record<NodeKind, string> = {
  local: "本机",
  remote: "远端",
  unbound: "未绑定设备",
};
const nodeStatusText = { ONLINE: "在线", OFFLINE: "离线", DISABLED: "已停用" };

function lineOf(bot: MapBot, show: (text: string) => string) {
  if (bot.availability === "unavailable" || bot.availability === "unknown")
    return bot.reason;
  const work = bot.running[0];
  if (work) return `正在处理：${show(work.title)}`;
  return bot.runningCount > 0 ? "正在执行" : "待命";
}

/**
 * 小人行为同步：数据变化时只改目标，行走由定时器推进。
 * 减少动态时不跑动画，状态变化直接跳到结果位置。
 */
function useOffice(
  bots: MapBot[],
  cards: Card[],
  layout: Layout,
  reduce: boolean,
  show: (text: string) => string,
) {
  const actors = useRef(new Map<string, Actor>());
  const rngs = useRef(new Map<string, Rng>());
  const before = useRef(new Map<string, { mode: string; running: string[] }>());
  const owner = useRef<Layout | null>(null);
  const [, setFrame] = useState(0);
  const rngOf = (id: string) => {
    let rng = rngs.current.get(id);
    if (!rng) {
      rng = seededRng(id);
      rngs.current.set(id, rng);
    }
    return rng;
  };
  const settle = (actor: Actor) => {
    for (let i = 0; i < 60 && actor.stops.length; i++) {
      actor.wait = 0;
      tick(actor, layout, 1e5, rngOf(actor.id));
    }
    actor.wait = 0;
    actor.say = null;
    actor.moving = false;
  };

  useEffect(() => {
    // 房间或工位变了：全部重新入座/散开，不拿旧坐标凑合。
    if (owner.current !== layout) {
      owner.current = layout;
      actors.current.clear();
      before.current.clear();
    }
    const live = new Set(bots.map((b) => b.id));
    for (const id of [...actors.current.keys()])
      if (!live.has(id)) {
        actors.current.delete(id);
        before.current.delete(id);
      }
    for (const bot of bots) {
      const mode = modeOf(bot.availability);
      const running = bot.running.map((c) => c.id);
      const actor = actors.current.get(bot.id);
      const last = before.current.get(bot.id);
      before.current.set(bot.id, { mode, running });
      if (!actor || !last) {
        actors.current.set(bot.id, spawn(bot, layout, rngOf(bot.id)));
        continue;
      }
      const finished = last.running.filter((id) => !running.includes(id));
      const started = running.filter((id) => !last.running.includes(id));
      const wasWorking = last.mode === "working";
      const errand: Errand = {};
      if (
        wasWorking &&
        (mode === "idle" || (mode === "working" && finished.length))
      )
        errand.register = registerText(finished, cards, show);
      if (mode === "working" && (!wasWorking || started.length)) {
        const next = bot.running.find((c) => started.includes(c.id));
        errand.claim = next
          ? `领取：${show(excerpt(next.title, 12).text)}`
          : "领取任务";
      }
      if (mode === last.mode && !errand.register && !errand.claim) continue;
      setMode(actor, layout, mode, errand);
      if (reduce) settle(actor);
    }
    setFrame((n) => n + 1);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [bots, layout]);

  useEffect(() => {
    if (reduce) return;
    let last = performance.now();
    const timer = setInterval(() => {
      const now = performance.now();
      const dt = Math.min(250, now - last);
      last = now;
      if (
        typeof document !== "undefined" &&
        document.visibilityState === "hidden"
      )
        return;
      for (const actor of actors.current.values())
        tick(actor, layout, dt, rngOf(actor.id));
      setFrame((n) => n + 1);
    }, 100);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reduce, layout]);

  return actors.current;
}

/** 布局里的矩形是逻辑坐标，落到 DOM 时转成绝对定位。 */
function box(r: Rect): React.CSSProperties {
  return { left: r.x, top: r.y, width: r.w, height: r.h };
}

export interface View {
  zoom: number;
  x: number;
  y: number;
}

export function OfficeScene({
  bots,
  cards,
  groups,
  delivered,
  topology,
  selectedBot,
  boardSelected,
  onSelectBot,
  onSelectBoard,
  bubbles,
  flash,
  show,
  reduce,
  view,
  onView,
}: {
  bots: MapBot[];
  /** 当前范围内的全部卡片（含已结束），用来说明小人登记了什么。 */
  cards: Card[];
  groups: WorkMapData["groups"];
  delivered: number;
  topology: Topology | null;
  selectedBot: string | null;
  boardSelected: boolean;
  onSelectBot: (id: string) => void;
  onSelectBoard: () => void;
  bubbles: MapEvent[];
  flash: boolean;
  show: (text: string) => string;
  reduce: boolean;
  view: View;
  onView: (update: (view: View) => View) => void;
}) {
  // 布局只取决于“谁在哪台设备上”，不随忙闲变化。
  const signature = bots
    .map((b) => `${b.id}|${b.nodeId}|${b.nodeKind}|${b.nodeName}`)
    .join(",");
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const layout = useMemo(() => buildLayout(bots), [signature]);
  const actors = useOffice(bots, cards, layout, reduce, show);
  const botById = new Map(bots.map((b) => [b.id, b]));
  const nodes = new Map((topology?.nodes ?? []).map((n) => [n.id, n]));

  const frame = useRef<HTMLDivElement>(null);
  const drag = useRef<{ x: number; y: number } | null>(null);
  const [width, setWidth] = useState(960);
  useEffect(() => {
    const el = frame.current;
    if (!el) return;
    const measure = () => setWidth(el.clientWidth || 960);
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, []);
  const fit = Math.min(1.5, Math.max(0.3, width / layout.width));
  const scale = fit * view.zoom;
  const offsetX = (width - layout.width * scale) / 2 + view.x;
  const boardCount = Object.values(groups).reduce((n, g) => n + g.length, 0);
  const localCount = bots.filter((b) => b.nodeKind === "local").length;
  const remoteCount = bots.filter((b) => b.nodeKind === "remote").length;

  function pan(e: React.PointerEvent<HTMLDivElement>) {
    if (!drag.current) return;
    const dx = e.clientX - drag.current.x;
    const dy = e.clientY - drag.current.y;
    drag.current = { x: e.clientX, y: e.clientY };
    onView((v) => ({ ...v, x: v.x + dx, y: v.y + dy }));
  }

  return (
    <div
      ref={frame}
      className="map-viewport"
      data-pan
      style={{
        height: Math.min(780, Math.max(380, layout.height * fit + 8)),
      }}
      onPointerDown={(e) => {
        if (!(e.target as HTMLElement).closest("button")) {
          drag.current = { x: e.clientX, y: e.clientY };
          e.currentTarget.setPointerCapture?.(e.pointerId);
        }
      }}
      onPointerMove={pan}
      onPointerUp={() => (drag.current = null)}
      onPointerCancel={() => (drag.current = null)}
    >
      <div
        className="house"
        style={{
          width: layout.width,
          height: layout.height,
          transform: `translate(${offsetX}px, ${view.y}px) scale(${scale})`,
        }}
      >
        <div className="house-title" aria-hidden="true">
          <strong>niucai 办公室</strong>
          <small>
            本机 {localCount} 位 · 远端 {remoteCount} 位
          </small>
        </div>
        <div className="rug" aria-hidden="true" />
        <div className="sofa" aria-hidden="true">
          休息区
        </div>
        <div className="coffee" aria-hidden="true">
          咖啡
        </div>
        <div className="plant plant-a" aria-hidden="true" />
        <div className="plant plant-b" aria-hidden="true" />

        <button
          className={`whiteboard${flash ? " flash" : ""}${boardSelected ? " selected" : ""}`}
          style={box(layout.board)}
          aria-label={`共享白板：${boardGroups
            .filter((g) => g.key !== "draft")
            .map((g) => `${g.label} ${groups[g.key].length}`)
            .join("，")}`}
          onClick={onSelectBoard}
        >
          <strong>共享白板 · 共 {boardCount} 张</strong>
          <span className="board-cols" aria-hidden="true">
            {boardGroups
              .filter((g) => g.key !== "draft")
              .map((g) => (
                <span className="board-col" key={g.key}>
                  <small>
                    {g.label} {groups[g.key].length}
                  </small>
                  <span className="notes">
                    {groups[g.key].slice(0, 8).map((c) => (
                      <i
                        key={c.id}
                        style={{ background: noteColor[c.status] }}
                      />
                    ))}
                  </span>
                </span>
              ))}
          </span>
          {bubbles.length > 0 && (
            <span className="bubbles" aria-hidden="true">
              {bubbles.map((b) => (
                <em key={b.key}>{show(excerpt(b.cardTitle, 16).text)}</em>
              ))}
            </span>
          )}
        </button>
        <button
          className="shelf"
          style={box(layout.shelf)}
          aria-label={`交付架：${delivered} 项有结果的完成工作`}
          onClick={onSelectBoard}
        >
          <strong>交付架</strong>
          <small>{delivered ? `${delivered} 项结果` : "暂无结果"}</small>
        </button>

        {layout.rooms.map((room) => {
          const node = nodes.get(room.nodeId);
          const engines = [
            ...new Set(
              room.desks
                .map((d) => botById.get(d.botId)?.engine)
                .filter(Boolean),
            ),
          ];
          return (
            <div
              key={room.id}
              className={`room ${room.kind}`}
              style={box(room.rect)}
              aria-label={`${room.title}，${kindText[room.kind]}`}
            >
              <header>
                <strong title={room.title}>{room.title}</strong>
                <span className="room-badge">{kindText[room.kind]}</span>
                {node && (
                  <small className={node.status === "ONLINE" ? "ok" : "bad"}>
                    {nodeStatusText[node.status]}
                  </small>
                )}
                {engines.length > 0 && <small>{engines.join(" / ")}</small>}
              </header>
              <span
                className="door"
                aria-hidden="true"
                style={{ left: room.doorX - room.rect.x - 28 }}
              />
            </div>
          );
        })}

        {layout.rooms.flatMap((room) =>
          room.desks.map((d) => {
            const bot = botById.get(d.botId);
            if (!bot) return null;
            const selected = selectedBot === bot.id;
            return (
              <button
                key={`desk-${bot.id}`}
                className={`desk ${bot.availability}${selected ? " selected" : ""}`}
                style={{
                  left: d.cell.x + 6,
                  top: d.desk.y,
                  width: d.cell.w - 12,
                  zIndex: Math.round(d.desk.y + d.desk.h),
                }}
                aria-label={`${bot.name} 的工位，${lineOf(bot, show)}`}
                onClick={() => onSelectBot(bot.id)}
              >
                <span className="desk-top" aria-hidden="true">
                  {bot.availability === "working" && (
                    <span className="laptop" />
                  )}
                </span>
                <strong title={bot.name}>{bot.name}</strong>
                <small>{lineOf(bot, show)}</small>
                {bot.blocked.length + bot.review.length > 0 && (
                  <span className="seat-flag">
                    {bot.blocked.length ? `受阻 ${bot.blocked.length}` : ""}
                    {bot.review.length ? ` 待验收 ${bot.review.length}` : ""}
                  </span>
                )}
              </button>
            );
          }),
        )}

        {bots.map((bot) => {
          const actor = actors.get(bot.id);
          if (!actor) return null;
          const selected = selectedBot === bot.id;
          return (
            <button
              key={`actor-${bot.id}`}
              className={`actor ${bot.availability}${actor.moving ? " walking" : ""}${actor.sitting ? " sitting" : ""}${selected ? " selected" : ""}`}
              style={{
                transform: `translate(${actor.x - 20}px, ${actor.y - 40}px)`,
                zIndex: Math.round(actor.y),
              }}
              // 工位按钮负责键盘与读屏；小人本身只是可点击的画面元素。
              aria-hidden="true"
              tabIndex={-1}
              onClick={() => onSelectBot(bot.id)}
            >
              {actor.say && <span className="say">{actor.say}</span>}
              <span
                className="flip"
                style={{ transform: `scaleX(${actor.facing})` }}
              >
                <Sprite color={colorFor(bot.id)} />
              </span>
              {!actor.sitting && <span className="tag">{bot.name}</span>}
            </button>
          );
        })}
      </div>
    </div>
  );
}
