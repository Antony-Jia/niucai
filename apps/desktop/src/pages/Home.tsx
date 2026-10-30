import { useEffect, useMemo, useRef, useState } from "react";
import { ArrowRight, ArrowUp, Search, Trash2 } from "lucide-react";
import { api, request } from "../lib/api";
import { boardApi } from "../lib/board";
import { goalsApi } from "../lib/goals";
import type { Topology } from "../components/NodesPanel";
import { useApp, useCommand, useData } from "../lib/state";
import { activeMention, insertMention, mentionedBots } from "../lib/mentions";
import { removeFinished, removeItem } from "../lib/remove-item";
import { availabilityText, projectWorkMap } from "../lib/work-map";
import {
  botReadiness,
  chooseRoute,
  displayText,
  filterItems,
  filterText,
  isRemovable,
  type NewTaskRoute,
  type TaskFilter,
  type TaskItem,
} from "../lib/task-view";
import { useTaskItems } from "../lib/use-task-items";
import {
  ConnectionEmpty,
  Empty,
  ErrorNotice,
  Header,
  Loading,
  time,
} from "../components/ui";
import { WorkMap } from "../components/WorkMap";
import { BotRoster } from "../components/BotRoster";

type View = "list" | "map";
const VIEW_KEY = "niucai.home-view";
const filters = Object.keys(filterText) as TaskFilter[];

function savedView(): View {
  try {
    return localStorage.getItem(VIEW_KEY) === "map" ? "map" : "list";
  } catch {
    return "list";
  }
}

const routeHint = (route: NewTaskRoute | null, picked: string[]) =>
  !route
    ? "正在读取工作空间配置…"
    : route.kind === "goal"
      ? picked.length
        ? `将交给 @kernel 规划，优先分派给 ${picked.map((n) => `@${n}`).join("、")}。`
        : "将交给 @kernel 规划，并分派给可用的助手。输入 @ 可指定助手。"
      : route.kind === "conversation"
        ? picked.length
          ? "在会话中执行时不能指定助手，@ 将被忽略。"
          : "将在会话中执行，需要时会向你确认。"
        : route.reason;

interface Attempt {
  text: string;
  route: NewTaskRoute["kind"];
  clientId: string;
  bots: string[];
  createdId?: string;
}

function StatusBadge({ item }: { item: TaskItem }) {
  return (
    <span className={`task-status ${item.status}`}>
      {displayText[item.status]}
    </span>
  );
}

function TaskRow({
  item,
  onOpen,
  onRemove,
  removing,
}: {
  item: TaskItem;
  onOpen: () => void;
  onRemove: () => void;
  removing: boolean;
}) {
  return (
    <div className="home-task-row">
      <button className="home-task" onClick={onOpen}>
        <span className="home-task-main">
          <strong>{item.title}</strong>
          <small>{item.activity || displayText[item.status]}</small>
        </span>
        <span className="home-task-meta">
          <StatusBadge item={item} />
          <small>{time(item.updatedAt)}</small>
        </span>
      </button>
      {isRemovable(item) && (
        <button
          className="home-task-remove"
          aria-label={`删除任务 ${item.title}`}
          title="删除这条已结束的任务"
          disabled={removing}
          onClick={onRemove}
        >
          <Trash2 size={14} />
        </button>
      )}
    </div>
  );
}

export function Home({
  onTask,
  onStandalone,
  onGoal,
  focusGoal,
  onFocusGoal,
}: {
  onTask?: (id: string) => void;
  onStandalone?: (id: string) => void;
  onGoal?: (id: string | null) => void;
  focusGoal?: string | null;
  onFocusGoal?: (id: string | null) => void;
}) {
  const { connected, setPage, demo } = useApp();
  const { items, counts, stale, loading, errors, goalsFailed } = useTaskItems();
  const diagnostics = useData(["kernel-diagnostics"], () =>
    request<{
      features?: { orchestrator?: boolean; legacy_execution?: boolean };
    }>("/api/kernel/diagnostics"),
  );
  const bots = useData(["bots"], boardApi.bots);
  const topology = useData(["topology"], () =>
    request<Topology>("/api/topology"),
  );
  const board = useData(["board"], boardApi.board);
  const [text, setText] = useState("");
  const [caret, setCaret] = useState(0);
  const [menuIndex, setMenuIndex] = useState(0);
  const [menuClosed, setMenuClosed] = useState(false);
  const [filter, setFilter] = useState<TaskFilter>("all");
  const [query, setQuery] = useState("");
  const [view, setView] = useState<View>(savedView);
  const attempt = useRef<Attempt | undefined>(undefined);
  const input = useRef<HTMLTextAreaElement>(null);
  const mapAnchor = useRef<HTMLElement>(null);
  const botList = useMemo(() => bots.data ?? [], [bots.data]);
  const pickable = useMemo(
    () => botList.filter((bot) => bot.enabled).map((bot) => bot.name),
    [botList],
  );
  const picked = mentionedBots(text, pickable);
  const roster = useMemo(
    () =>
      projectWorkMap({
        bots: botList,
        cards: board.data?.cards ?? [],
        topology: topology.data ?? null,
        stale: stale || board.isError || topology.isError,
        topologyUnknown: topology.isError || topology.data === undefined,
      }),
    [
      botList,
      board.data,
      board.isError,
      topology.data,
      topology.isError,
      stale,
    ],
  );
  const mention = menuClosed ? null : activeMention(text, caret);
  const suggestions = mention
    ? roster.bots.filter(
        (bot) =>
          pickable.includes(bot.name) &&
          bot.name.toLowerCase().startsWith(mention.query.toLowerCase()),
      )
    : [];
  const features = diagnostics.data?.features;
  const botsKnown =
    (bots.data !== undefined || bots.isError) &&
    (topology.data !== undefined || topology.isError);
  const route: NewTaskRoute | null =
    diagnostics.data && botsKnown
      ? chooseRoute({
          orchestrator: features?.orchestrator,
          legacy: features?.legacy_execution,
          bots: botReadiness(bots.data, topology.data),
        })
      : null;
  useEffect(() => {
    if (focusGoal) setView("map");
  }, [focusGoal]);
  function chooseView(next: View) {
    setView(next);
    if (next === "list") onFocusGoal?.(null);
    try {
      localStorage.setItem(VIEW_KEY, next);
    } catch {
      /* 记住视图只是便利功能 */
    }
  }
  function open(item: TaskItem) {
    // 三种来源各有自己的详情入口；独立任务没有会话，不能走会话接口。
    if (item.sourceKind === "goal") {
      onGoal?.(item.sourceId);
      setPage("goals");
    } else if (item.sourceKind === "task") {
      onStandalone?.(item.sourceId);
      setPage("task");
    } else {
      onTask?.(item.sourceId);
      setPage("computer");
    }
  }
  const start = useCommand(async (content: string) => {
    // 同一段文字重试时沿用 clientId 和已创建的对象，且不会切换路径。
    if (!attempt.current || attempt.current.text !== content) {
      if (!route || route.kind === "draft")
        throw new Error(route ? route.reason : "正在读取工作空间配置");
      attempt.current = {
        text: content,
        route: route.kind,
        clientId: crypto.randomUUID(),
        bots: route.kind === "goal" ? picked : [],
      };
    }
    const current = attempt.current;
    if (current.route === "goal") {
      const goal = current.createdId
        ? { id: current.createdId }
        : await goalsApi.create(content, "", current.clientId, current.bots);
      current.createdId = goal.id;
      return { kind: "goal" as const, id: goal.id };
    }
    let conversationId = current.createdId;
    if (!conversationId) {
      const status = await request<{ enabled: boolean }>(
        "/api/collaboration/status",
      );
      let computerId: string | undefined;
      if (!status.enabled) {
        const computers = await api.computers();
        computerId = computers.find((c) => c.kind === "linux")?.id;
      }
      conversationId = (
        await api.createConversation(computerId, "main", ["main"])
      ).id;
      current.createdId = conversationId;
    }
    await api.sendMessage(conversationId, content, current.clientId);
    return { kind: "conversation" as const, id: conversationId };
  });
  function submit() {
    const content = text.trim();
    if (!content || start.isPending || !route || route.kind === "draft") return;
    start.mutate(content, {
      onSuccess: (result) => {
        attempt.current = undefined;
        setText("");
        if (result.kind === "goal") onGoal?.(result.id);
        else onTask?.(result.id);
        setPage(result.kind === "goal" ? "goals" : "computer");
      },
    });
  }
  const remove = useCommand(async (list: TaskItem[]) => {
    if (list.length === 1) {
      await removeItem(list[0]);
      return;
    }
    const { removed, failures } = await removeFinished(list);
    if (failures.length)
      throw new Error(
        `已删除 ${removed} 项，${failures.length} 项失败：${failures[0]}`,
      );
  });
  function removeOne(item: TaskItem) {
    if (confirm(`删除任务“${item.title}”？删除后不再出现在列表里。`))
      remove.mutate([item]);
  }
  function chooseMention(name: string) {
    const area = input.current;
    const at = area?.selectionStart ?? caret;
    const active = activeMention(text, at);
    const next = active
      ? insertMention(text, at, active, name)
      : // 从名册点选：追加到末尾，已经提到过就不重复。
        picked.includes(name)
        ? { text, caret: text.length }
        : (() => {
            const base = text && !/\s$/.test(text) ? `${text} ` : text;
            return {
              text: `${base}@${name} `,
              caret: base.length + name.length + 2,
            };
          })();
    setText(next.text);
    setCaret(next.caret);
    setMenuClosed(false);
    setMenuIndex(0);
    requestAnimationFrame(() => {
      area?.focus();
      area?.setSelectionRange(next.caret, next.caret);
    });
  }
  function openMap() {
    chooseView("map");
    requestAnimationFrame(() =>
      mapAnchor.current?.scrollIntoView?.({
        block: "start",
        behavior: "smooth",
      }),
    );
  }
  if (!connected)
    return (
      <>
        <Header
          eyebrow="TASKS"
          title="任务"
          description="说出目标，剩下的交给助手。"
        />
        <ConnectionEmpty />
      </>
    );
  const attention = items.filter((item) => item.pending.length > 0);
  const shown = filterItems(items, filter, query);
  const removable = items.filter(isRemovable);
  const countFor = (key: TaskFilter) => filterItems(items, key).length;
  return (
    <div className="home-page">
      <Header
        eyebrow="TASKS"
        title="今天想完成什么？"
        description="说出目标，助手会持续推进；需要你时会在这里提醒。"
      />
      <form
        className="home-composer panel"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        <div className="mention-field">
          <textarea
            ref={input}
            aria-label="任务目标"
            aria-autocomplete="list"
            aria-expanded={suggestions.length > 0}
            placeholder="例如：整理本周竞品动态并写成一页摘要… 输入 @ 可指定助手，Ctrl + Enter 开始"
            value={text}
            maxLength={12000}
            onChange={(e) => {
              setText(e.target.value);
              setCaret(e.target.selectionStart);
              setMenuClosed(false);
              setMenuIndex(0);
            }}
            onSelect={(e) => setCaret(e.currentTarget.selectionStart)}
            onKeyDown={(e) => {
              if (suggestions.length > 0) {
                if (e.key === "ArrowDown" || e.key === "ArrowUp") {
                  e.preventDefault();
                  const step = e.key === "ArrowDown" ? 1 : -1;
                  setMenuIndex(
                    (menuIndex + step + suggestions.length) %
                      suggestions.length,
                  );
                  return;
                }
                if (e.key === "Enter" || e.key === "Tab") {
                  e.preventDefault();
                  chooseMention(
                    suggestions[Math.min(menuIndex, suggestions.length - 1)]
                      .name,
                  );
                  return;
                }
                if (e.key === "Escape") {
                  e.preventDefault();
                  setMenuClosed(true);
                  return;
                }
              }
              if (e.ctrlKey && e.key === "Enter") {
                e.preventDefault();
                submit();
              }
            }}
          />
          {suggestions.length > 0 && (
            <ul className="mention-menu" role="listbox" aria-label="指定助手">
              {suggestions.map((bot, index) => (
                <li
                  key={bot.id}
                  role="option"
                  aria-selected={index === menuIndex}
                  className={index === menuIndex ? "active" : ""}
                  onMouseDown={(e) => {
                    // 不让文本框失焦，否则光标位置会丢。
                    e.preventDefault();
                    chooseMention(bot.name);
                  }}
                >
                  <strong>@{bot.name}</strong>
                  <small>
                    {availabilityText[bot.availability]}
                    {bot.role ? ` · ${bot.role}` : ""}
                  </small>
                </li>
              ))}
            </ul>
          )}
        </div>
        <div className="home-composer-bar">
          <span
            className={
              route?.kind === "draft" ? "route-hint warn" : "route-hint"
            }
            role="status"
          >
            {routeHint(route, picked)}
          </span>
          <button
            className="primary"
            aria-label="开始任务"
            disabled={
              start.isPending ||
              !text.trim() ||
              !route ||
              route.kind === "draft"
            }
          >
            {start.isPending ? "正在创建…" : "开始任务"}
            <ArrowUp size={15} />
          </button>
        </div>
        {demo && (
          <small className="muted">
            示例模式：任务不会调用真实模型或设备。
          </small>
        )}
      </form>
      <BotRoster
        bots={roster.bots}
        summary={roster.summary}
        selected={picked}
        pickable={pickable}
        onPick={chooseMention}
        onOpenMap={openMap}
        mapOpen={view === "map"}
      />
      <ErrorNotice error={errors[0]} />
      {stale && (
        <p className="quiet-note" role="status">
          状态待同步，下面显示的是最后已知状态。
        </p>
      )}
      {goalsFailed && !demo && (
        <p className="quiet-note" role="status">
          暂时无法读取 Goal，列表可能不完整。
        </p>
      )}
      {attention.length > 0 && (
        <section className="panel attention-panel" aria-label="需要你处理">
          <div className="section-heading">
            <h2>需要你处理</h2>
            <span className="count">
              {counts.tasks} 项任务
              {counts.items !== counts.tasks ? ` · ${counts.items} 件事` : ""}
            </span>
          </div>
          {attention.map((item) => (
            <div className="attention-item" key={item.key}>
              <div>
                <strong>{item.title}</strong>
                {item.pending.map((p) => (
                  <small key={p.id}>{p.reason}</small>
                ))}
              </div>
              <button onClick={() => open(item)}>
                查看并处理 <ArrowRight size={14} />
              </button>
            </div>
          ))}
        </section>
      )}
      <section className="panel" aria-label="我的任务" ref={mapAnchor}>
        <div className="section-heading">
          <h2>我的任务</h2>
          <div className="view-toggle" role="group" aria-label="任务视图">
            {(["list", "map"] as View[]).map((key) => (
              <button
                key={key}
                aria-pressed={view === key}
                className={view === key ? "active" : ""}
                onClick={() => chooseView(key)}
              >
                {key === "list" ? "列表" : "地图"}
              </button>
            ))}
          </div>
        </div>
        {view === "list" ? (
          <>
            <div className="home-filters">
              <div role="group" aria-label="任务筛选">
                {filters.map((key) => (
                  <button
                    key={key}
                    aria-pressed={filter === key}
                    className={filter === key ? "active" : ""}
                    onClick={() => setFilter(key)}
                  >
                    {filterText[key]} {countFor(key)}
                  </button>
                ))}
              </div>
              <label className="search">
                <Search size={14} />
                <input
                  aria-label="搜索任务"
                  placeholder="搜索任务"
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                />
              </label>
              {removable.length > 0 && (
                <button
                  disabled={remove.isPending}
                  onClick={() => {
                    if (
                      confirm(
                        `清除 ${removable.length} 项已结束的任务？进行中的不会受影响。`,
                      )
                    )
                      remove.mutate(removable);
                  }}
                >
                  <Trash2 size={14} /> 清除已结束 {removable.length}
                </button>
              )}
            </div>
            {loading ? (
              <Loading />
            ) : shown.length === 0 ? (
              <Empty
                title={items.length === 0 ? "还没有任务" : "没有符合条件的任务"}
                description={
                  items.length === 0
                    ? "在上面写下一个目标，任务会出现在这里。"
                    : "换一个筛选或搜索词试试。"
                }
              />
            ) : (
              shown.map((item) => (
                <TaskRow
                  key={item.key}
                  item={item}
                  onOpen={() => open(item)}
                  onRemove={() => removeOne(item)}
                  removing={remove.isPending}
                />
              ))
            )}
          </>
        ) : (
          <WorkMap
            items={items}
            pendingTasks={counts.tasks}
            focusGoal={focusGoal ?? null}
            onFocusGoal={(id) => onFocusGoal?.(id)}
            onShowAttention={() => {
              chooseView("list");
              setFilter("attention");
            }}
            onOpen={open}
            stale={stale}
          />
        )}
      </section>
    </div>
  );
}
