import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { EyeOff, Minus, Plus, Scan } from "lucide-react";
import { request } from "../lib/api";
import { boardApi, statusText, type Card } from "../lib/board";
import { goalsApi } from "../lib/goals";
import { useApp, useData } from "../lib/state";
import { connectionKey } from "../lib/types";
import type { TaskItem } from "../lib/task-view";
import {
  availabilityText,
  boardGroups,
  createEventTracker,
  eventLabel,
  excerpt,
  hiddenText,
  pickBubbles,
  projectWorkMap,
  SEATS_PER_ZONE,
  type MapBot,
  type MapEvent,
} from "../lib/work-map";
import type { Topology } from "./NodesPanel";
import { ErrorNotice, time } from "./ui";
import { CardDetailView } from "../pages/Whiteboard";
import { noteColor, OfficeScene, type View } from "./OfficeScene";

const PREF_KEY = "niucai.work-map";
interface Prefs {
  reduce: boolean;
  hide: boolean;
}
function loadPrefs(): Prefs {
  try {
    const raw = JSON.parse(localStorage.getItem(PREF_KEY) || "{}");
    return { reduce: raw.reduce === true, hide: raw.hide === true };
  } catch {
    return { reduce: false, hide: false };
  }
}
function systemReduced() {
  try {
    return window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
  } catch {
    return false;
  }
}

type Selection =
  | { kind: "bot"; id: string }
  | { kind: "board" }
  | { kind: "event"; key: string }
  | null;

export function WorkMap(props: Props) {
  const { profile, demo } = useApp();
  // 切换连接时整张地图（选择、事件缓存、动画队列）一并丢弃。
  const identity = demo ? "demo" : profile ? connectionKey(profile) : "none";
  return <WorkMapView key={identity} {...props} />;
}

interface Props {
  items: TaskItem[];
  pendingTasks: number;
  focusGoal: string | null;
  onFocusGoal: (id: string | null) => void;
  onShowAttention: () => void;
  onOpen: (item: TaskItem) => void;
  stale: boolean;
}

function WorkMapView({
  items,
  pendingTasks,
  focusGoal,
  onFocusGoal,
  onShowAttention,
  onOpen,
  stale,
}: Props) {
  const { demo, setPage } = useApp();
  const bots = useData(["bots"], boardApi.bots);
  const topology = useData(["topology"], () =>
    request<Topology>("/api/topology"),
  );
  const board = useData(["board"], boardApi.board);
  const goal = useQuery({
    queryKey: ["goal", focusGoal],
    queryFn: () => goalsApi.get(focusGoal as string),
    enabled: !!focusGoal,
    refetchInterval: 10000,
  });
  const [includeClosed, setIncludeClosed] = useState(false);
  const [selection, setSelection] = useState<Selection>(null);
  const [cardId, setCardId] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [prefs, setPrefs] = useState<Prefs>(loadPrefs);
  const [view, setView] = useState<View>({ zoom: 1, x: 0, y: 0 });
  const [bubbles, setBubbles] = useState<MapEvent[]>([]);
  const [flash, setFlash] = useState(false);
  const tracker = useRef(createEventTracker());
  const reduce = prefs.reduce || systemReduced();
  const hide = prefs.hide;
  const show = (text: string) => (hide ? hiddenText : text);
  function setPref(next: Partial<Prefs>) {
    const merged = { ...prefs, ...next };
    setPrefs(merged);
    try {
      localStorage.setItem(PREF_KEY, JSON.stringify(merged));
    } catch {
      /* 偏好只是便利功能 */
    }
  }

  const goalItems = items.filter((item) => item.sourceKind === "goal");
  const scoped = !!focusGoal;
  const cards: Card[] | undefined = scoped
    ? goal.data?.board.cards
    : board.data?.cards;
  const failed =
    bots.isError ||
    board.isError ||
    topology.isError ||
    (scoped && goal.isError);
  const topologyUnknown = topology.isError || topology.data === undefined;
  const outdated = stale || failed;
  const loading = !cards || !bots.data;
  const data = useMemo(
    () =>
      projectWorkMap({
        bots: bots.data ?? [],
        cards: cards ?? [],
        topology: topology.data ?? null,
        onlyAssigned: scoped,
        includeClosed: includeClosed || scoped,
        stale: outdated,
        topologyUnknown,
      }),
    [
      bots.data,
      cards,
      topology.data,
      scoped,
      includeClosed,
      outdated,
      topologyUnknown,
    ],
  );
  const updatedAt = Math.max(
    board.dataUpdatedAt,
    bots.dataUpdatedAt,
    scoped ? goal.dataUpdatedAt : 0,
  );

  // 范围变化后重新建立基线，避免把另一个范围的历史当成新事件播放。
  useEffect(() => {
    tracker.current.reset();
    setBubbles([]);
    setSelection(null);
  }, [focusGoal, includeClosed]);
  useEffect(() => {
    // 数据还没读回来时 events 为空；此时建立基线会把随后到达的整批历史当成新事件。
    if (loading) return;
    const fresh = tracker.current.fresh(data.events);
    if (!fresh.length || reduce || outdated) return;
    if (
      typeof document !== "undefined" &&
      document.visibilityState === "hidden"
    )
      return;
    const picked = pickBubbles(fresh, 2);
    setBubbles(picked);
    setFlash(true);
    const timer = setTimeout(() => {
      setBubbles([]);
      setFlash(false);
    }, 4000);
    return () => clearTimeout(timer);
  }, [data.events, reduce, outdated, loading]);

  const botById = new Map(data.bots.map((b) => [b.id, b]));
  const selectedBot =
    selection?.kind === "bot" ? botById.get(selection.id) : undefined;
  const selectedEvent =
    selection?.kind === "event"
      ? data.events.find((e) => e.key === selection.key)
      : undefined;
  const botList = data.bots.filter(
    (b) => !search || b.name.toLowerCase().includes(search.toLowerCase()),
  );
  const goalItem = goalItems.find((item) => item.sourceId === focusGoal);

  const zoomBy = (delta: number) =>
    setView((v) => ({
      ...v,
      zoom: Math.min(2.2, Math.max(0.6, +(v.zoom + delta).toFixed(2))),
    }));

  const cardButton = (card: Card) => (
    <button
      key={card.id}
      className="map-link"
      onClick={() => setCardId(card.id)}
    >
      <span
        className="map-pill"
        style={{ background: noteColor[card.status] }}
      />
      {show(card.title)}
      <small>{statusText[card.status]}</small>
    </button>
  );

  return (
    <div className={`work-map${reduce ? " reduce-motion" : ""}`}>
      <div className="map-topbar">
        <div className="map-scope">
          <label>
            范围
            <select
              aria-label="地图范围"
              value={focusGoal ?? (includeClosed ? "all" : "active")}
              onChange={(e) => {
                const value = e.target.value;
                if (value === "active" || value === "all") {
                  onFocusGoal(null);
                  setIncludeClosed(value === "all");
                } else onFocusGoal(value);
              }}
            >
              <option value="active">全部进行中</option>
              <option value="all">全部状态</option>
              {goalItems.map((item) => (
                <option key={item.sourceId} value={item.sourceId}>
                  任务：{item.title}
                </option>
              ))}
            </select>
          </label>
          {demo && <span className="map-demo">示例数据 · 非真实助手</span>}
        </div>
        <button
          className={pendingTasks ? "map-attention" : "map-attention quiet"}
          onClick={onShowAttention}
        >
          {pendingTasks
            ? `需要你处理 ${pendingTasks} 项任务`
            : "没有需要你处理的任务"}
        </button>
      </div>
      {outdated && (
        <p className="quiet-note" role="status">
          状态待同步
          {updatedAt
            ? ` · 最后更新 ${time(new Date(updatedAt).toISOString())}`
            : ""}
          。画面停留在最后已知状态，不代表任务已结束。
        </p>
      )}
      <ErrorNotice error={bots.error ?? board.error ?? goal.error} />
      {scoped && goal.isError ? (
        <p className="quiet-note">
          此任务暂无协作地图。
          {goalItem && (
            <button className="link-button" onClick={() => onOpen(goalItem)}>
              打开任务详情
            </button>
          )}
        </p>
      ) : loading ? (
        <p className="muted">正在读取助手与白板…</p>
      ) : (
        <div className="map-body">
          <div className="map-stage">
            {data.bots.length === 0 ? (
              <div className="map-viewport">
                <div className="map-empty">
                  <p>办公室里还没有助手。</p>
                  <button onClick={() => setPage("settings")}>配置助手</button>
                </div>
              </div>
            ) : (
              <OfficeScene
                bots={data.bots}
                cards={cards ?? []}
                groups={data.groups}
                delivered={data.delivered.length}
                topology={topology.data ?? null}
                selectedBot={selection?.kind === "bot" ? selection.id : null}
                boardSelected={selection?.kind === "board"}
                onSelectBot={(id) => setSelection({ kind: "bot", id })}
                onSelectBoard={() => setSelection({ kind: "board" })}
                bubbles={bubbles}
                flash={flash}
                show={show}
                reduce={reduce}
                view={view}
                onView={setView}
              />
            )}
            <div className="map-toolbar">
              <span className="map-summary" role="status">
                正在工作 {data.summary.working} · 待命 {data.summary.idle} ·
                不可用 {data.summary.unavailable}
                {data.summary.unknown
                  ? ` · 状态未知 ${data.summary.unknown}`
                  : ""}
              </span>
              <div className="button-row">
                <button
                  aria-pressed={reduce}
                  disabled={systemReduced()}
                  onClick={() => setPref({ reduce: !prefs.reduce })}
                  title={systemReduced() ? "系统已开启减少动态" : undefined}
                >
                  减少动态
                </button>
                <button
                  aria-pressed={hide}
                  onClick={() => setPref({ hide: !hide })}
                >
                  <EyeOff size={14} />
                  隐藏工作内容
                </button>
                <button aria-label="缩小" onClick={() => zoomBy(-0.1)}>
                  <Minus size={14} />
                </button>
                <button aria-label="放大" onClick={() => zoomBy(0.1)}>
                  <Plus size={14} />
                </button>
                <button onClick={() => setView({ zoom: 1, x: 0, y: 0 })}>
                  <Scan size={14} />
                  回到全景
                </button>
              </div>
            </div>
          </div>
          <aside className="map-side" aria-label="协作详情">
            {selection === null && (
              <>
                <h3>协作动态</h3>
                {data.events.length === 0 ? (
                  <p className="muted">当前范围还没有协作记录。</p>
                ) : (
                  <ul className="map-events">
                    {data.events.slice(0, 12).map((e) => (
                      <li key={e.key}>
                        <button
                          className="map-link"
                          onClick={() =>
                            setSelection({ kind: "event", key: e.key })
                          }
                        >
                          <strong>{show(e.cardTitle)}</strong>
                          <span>
                            {eventLabel(e.kind)} · {time(e.at)}
                          </span>
                          <small>{show(excerpt(e.body).text)}</small>
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
                <h3>助手</h3>
                {data.bots.length > SEATS_PER_ZONE && (
                  <input
                    aria-label="搜索助手"
                    placeholder="搜索助手"
                    value={search}
                    onChange={(e) => setSearch(e.target.value)}
                  />
                )}
                <ul className="map-bots">
                  {botList.map((b) => (
                    <li key={b.id}>
                      <button
                        className="map-link"
                        onClick={() => {
                          setSelection({ kind: "bot", id: b.id });
                        }}
                      >
                        <strong>{b.name}</strong>
                        <span className={`avail ${b.availability}`}>
                          {availabilityText[b.availability]}
                          {b.runningCount > 1 ? ` ×${b.runningCount}` : ""}
                        </span>
                      </button>
                    </li>
                  ))}
                </ul>
              </>
            )}
            {selection && (
              <button
                className="text-button"
                onClick={() => setSelection(null)}
              >
                ← 返回动态
              </button>
            )}
            {selectedBot && (
              <BotDetail
                bot={selectedBot}
                show={show}
                onCard={setCardId}
                onSettings={() => setPage("settings")}
                onTask={goalItem ? () => onOpen(goalItem) : undefined}
                outdated={outdated}
              />
            )}
            {selection?.kind === "bot" && !selectedBot && (
              <p className="muted">这位助手不在当前范围内。</p>
            )}
            {selection?.kind === "board" && (
              <div>
                <h3>白板</h3>
                {boardGroups.map((g) => (
                  <details
                    key={g.key}
                    open={g.key !== "closed" && g.key !== "draft"}
                  >
                    <summary>
                      {g.label} {data.groups[g.key].length}
                    </summary>
                    {data.groups[g.key].length === 0 ? (
                      <p className="muted">没有卡片</p>
                    ) : (
                      data.groups[g.key].map(cardButton)
                    )}
                  </details>
                ))}
              </div>
            )}
            {selectedEvent && (
              <div>
                <h3>{show(selectedEvent.cardTitle)}</h3>
                <p>
                  {eventLabel(selectedEvent.kind)} · {time(selectedEvent.at)}
                </p>
                <p className="preserve">{show(selectedEvent.body)}</p>
                <small className="muted">
                  发布者以卡片详情中的记录为准；这里不推断谁已阅读。
                </small>
                <button onClick={() => setCardId(selectedEvent.cardId)}>
                  查看原文与卡片详情
                </button>
              </div>
            )}
            {selection?.kind === "event" && !selectedEvent && (
              <p className="muted">这条记录已不在当前范围内。</p>
            )}
          </aside>
        </div>
      )}
      {cardId && (
        <CardDetailView id={cardId} readOnly onClose={() => setCardId(null)} />
      )}
    </div>
  );
}

function BotDetail({
  bot,
  show,
  onCard,
  onSettings,
  onTask,
  outdated,
}: {
  bot: MapBot;
  show: (text: string) => string;
  onCard: (id: string) => void;
  onSettings: () => void;
  onTask?: () => void;
  outdated: boolean;
}) {
  const list = (title: string, cards: Card[]) =>
    cards.length > 0 && (
      <>
        <h4>{title}</h4>
        <ul>
          {cards.map((c) => (
            <li key={c.id}>
              <button className="map-link" onClick={() => onCard(c.id)}>
                {show(c.title)}
                {c.latest && (
                  <small>{show(excerpt(c.latest.body, 50).text)}</small>
                )}
              </button>
            </li>
          ))}
        </ul>
      </>
    );
  const offline = bot.availability === "unavailable";
  return (
    <div className="bot-detail">
      <h3>
        {bot.name} <small>#{bot.shortId}</small>
      </h3>
      <p>
        <span className={`avail ${bot.availability}`}>
          {availabilityText[bot.availability]}
        </span>{" "}
        · {bot.reason}
      </p>
      <dl>
        <dt>角色</dt>
        <dd>{bot.role || "未填写"}</dd>
        <dt>引擎</dt>
        <dd>
          {bot.engine}
          {bot.model ? ` · ${bot.model}` : ""}
          {bot.reasoningEffort ? ` · ${bot.reasoningEffort}` : ""}
        </dd>
        <dt>执行设备</dt>
        <dd>
          {bot.nodeName || "未知"}
          {bot.nodeKind === "local"
            ? "（本机）"
            : bot.nodeKind === "remote"
              ? "（远端）"
              : ""}
        </dd>
        <dt>并发</dt>
        <dd>
          {bot.runningCount} / {bot.concurrency}
        </dd>
        {bot.tags.length > 0 && (
          <>
            <dt>标签</dt>
            <dd>{bot.tags.join("、")}</dd>
          </>
        )}
        {bot.skills.length + bot.mcpServers.length > 0 && (
          <>
            <dt>技能 / MCP</dt>
            <dd>{[...bot.skills, ...bot.mcpServers].join("、")}</dd>
          </>
        )}
      </dl>
      {(offline || outdated) && bot.running.length > 0 && (
        <p className="muted">以下为最后已知状态，可能已经变化。</p>
      )}
      {list(`进行中 ${bot.runningCount}`, bot.running)}
      {bot.runningCount > bot.running.length && (
        <p className="muted">
          另有 {bot.runningCount - bot.running.length}{" "}
          项执行未关联到当前范围内的卡片。
        </p>
      )}
      {list("受阻", bot.blocked)}
      {list("待验收", bot.review)}
      {list("最近完成", bot.finished)}
      {bot.running.length + bot.blocked.length + bot.review.length === 0 && (
        <p className="muted">当前没有正在处理的工作。</p>
      )}
      <div className="button-row">
        {onTask && <button onClick={onTask}>查看任务</button>}
        <button onClick={onSettings}>配置助手</button>
      </div>
    </div>
  );
}
