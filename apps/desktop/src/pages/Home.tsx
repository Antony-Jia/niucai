import { useEffect, useRef, useState } from "react";
import { ArrowRight, ArrowUp, Search } from "lucide-react";
import { api, request } from "../lib/api";
import { boardApi } from "../lib/board";
import { goalsApi } from "../lib/goals";
import type { Topology } from "../components/NodesPanel";
import { useApp, useCommand, useData } from "../lib/state";
import {
  botReadiness,
  chooseRoute,
  displayText,
  filterItems,
  filterText,
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

const routeHint = (route: NewTaskRoute | null) =>
  !route
    ? "正在读取工作空间配置…"
    : route.kind === "goal"
      ? "将交给 @kernel 规划，并分派给可用的助手。"
      : route.kind === "conversation"
        ? "将在会话中执行，需要时会向你确认。"
        : route.reason;

interface Attempt {
  text: string;
  route: NewTaskRoute["kind"];
  clientId: string;
  createdId?: string;
}

function StatusBadge({ item }: { item: TaskItem }) {
  return (
    <span className={`task-status ${item.status}`}>
      {displayText[item.status]}
    </span>
  );
}

function TaskRow({ item, onOpen }: { item: TaskItem; onOpen: () => void }) {
  return (
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
  const [text, setText] = useState("");
  const [filter, setFilter] = useState<TaskFilter>("all");
  const [query, setQuery] = useState("");
  const [view, setView] = useState<View>(savedView);
  const attempt = useRef<Attempt | undefined>(undefined);
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
      };
    }
    const current = attempt.current;
    if (current.route === "goal") {
      const goal = current.createdId
        ? { id: current.createdId }
        : await goalsApi.create(content, "", current.clientId);
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
        <textarea
          aria-label="任务目标"
          placeholder="例如：整理本周竞品动态并写成一页摘要… Ctrl + Enter 开始"
          value={text}
          maxLength={12000}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.ctrlKey && e.key === "Enter") {
              e.preventDefault();
              submit();
            }
          }}
        />
        <div className="home-composer-bar">
          <span
            className={
              route?.kind === "draft" ? "route-hint warn" : "route-hint"
            }
            role="status"
          >
            {routeHint(route)}
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
      <section className="panel" aria-label="我的任务">
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
                <TaskRow key={item.key} item={item} onOpen={() => open(item)} />
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
