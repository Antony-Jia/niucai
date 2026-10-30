import type { Bot, Card, CardStatus } from "./board";
import type { Topology } from "../components/NodesPanel";

/**
 * Bot 工作地图投影层：只读，把 Bot、节点和白板卡片整理成地图与等价列表所需的数据。
 * 不持有任务状态机，不推断“已读”“已开始”这类没有证据的事实。
 */
export type Availability = "working" | "idle" | "unavailable" | "unknown";

export const availabilityText: Record<Availability, string> = {
  working: "工作中",
  idle: "待命",
  unavailable: "不可用",
  unknown: "状态未知",
};

export const SEATS_PER_ZONE = 8;

export interface MapBot {
  id: string;
  name: string;
  /** 重名时用于区分的短标识。 */
  shortId: string;
  role: string;
  engine: string;
  nodeName: string;
  availability: Availability;
  reason: string;
  /** 正在执行的卡片；并发时同一个角色列出多项。 */
  running: Card[];
  blocked: Card[];
  review: Card[];
  /** Bot 自报的执行数与卡片对不上时，以较大值提示 ×N。 */
  runningCount: number;
  zone: number;
  seat: number;
  /** 所在设备 ID；未绑定设备为空串。 */
  nodeId: string;
  /** 办公室里的房间归类：本机 / 远端（VPS、Hermes 等）/ 未绑定设备。 */
  nodeKind: NodeKind;
  model: string;
  reasoningEffort: string;
  concurrency: number;
  skills: string[];
  mcpServers: string[];
  tags: string[];
  /** 最近几张由该 Bot 执行并已结束的卡片，用来在详情里显示“做完了什么”。 */
  finished: Card[];
}

export type NodeKind = "local" | "remote" | "unbound";
const localTypes = ["windows_local", "local_docker"];
export const nodeKindOf = (
  resourceType: string | undefined,
  hasNode: boolean,
): NodeKind =>
  !hasNode
    ? "unbound"
    : resourceType && localTypes.includes(resourceType)
      ? "local"
      : "remote";

export type BoardGroupKey =
  "draft" | "ready" | "running" | "attention" | "closed";
export const boardGroups: {
  key: BoardGroupKey;
  label: string;
  statuses: CardStatus[];
}[] = [
  { key: "ready", label: "待领取", statuses: ["READY", "WAITING_APPROVAL"] },
  { key: "running", label: "进行中", statuses: ["RUNNING"] },
  { key: "attention", label: "需处理", statuses: ["BLOCKED", "REVIEW"] },
  { key: "closed", label: "已结束", statuses: ["DONE", "FAILED", "CANCELLED"] },
  { key: "draft", label: "草稿", statuses: ["DRAFT"] },
];

export interface MapEvent {
  /** 优先用后端事件 ID（卡片ID#事件ID）；没有事件 ID 的活动提示用 卡片ID:种类:时间。 */
  key: string;
  cardId: string;
  cardTitle: string;
  kind: string;
  body: string;
  at: string;
  actor?: string;
  mentions?: string[];
}

export interface WorkMapData {
  bots: MapBot[];
  zones: number;
  groups: Record<BoardGroupKey, Card[]>;
  /** 有真实结果摘要的已完成卡片。 */
  delivered: Card[];
  events: MapEvent[];
  summary: {
    working: number;
    idle: number;
    unavailable: number;
    unknown: number;
  };
}

const closedStatuses: CardStatus[] = ["DONE", "FAILED", "CANCELLED"];
export const isClosed = (status: CardStatus) => closedStatuses.includes(status);

export const eventKindText: Record<string, string> = {
  comment: "评论",
  progress: "更新进度",
  status: "状态变化",
  question: "提问",
  answer: "回答",
  result: "提交结果",
  review: "验收",
};
export const eventLabel = (kind: string) => eventKindText[kind] ?? "更新";

const stamp = (value: string | undefined | null) => {
  if (!value) return 0;
  const parsed = Date.parse(value.endsWith("Z") ? value : value + "Z");
  return Number.isNaN(parsed) ? 0 : parsed;
};

/**
 * 卡片的执行者：公共池认领的卡片没有 assignee，只能看 Attempt。
 * 执行中取当前 Attempt；等待回答/验收时当前 Attempt 已清空，取最近一次。
 */
function executorOf(card: Card): string | null {
  if (card.status === "RUNNING") return card.current_job?.bot_id ?? null;
  if (card.status === "BLOCKED" || card.status === "REVIEW")
    return card.current_job?.bot_id ?? card.last_job?.bot_id ?? null;
  return null;
}

export interface ProjectInput {
  bots: Bot[];
  cards: Card[];
  topology?: Topology | null;
  /** 只展示在这些卡片上有分配的 Bot：范围为单个 Goal 时传入。 */
  onlyAssigned?: boolean;
  includeClosed?: boolean;
  /** 数据过期：不推断 Bot 能接新任务。 */
  stale?: boolean;
  /** 设备信息读取失败或还没读到：不能据此断言绑定设备的 Bot 在线或待命。 */
  topologyUnknown?: boolean;
}

export function projectWorkMap(input: ProjectInput): WorkMapData {
  const { topology, stale } = input;
  const cards = input.includeClosed
    ? input.cards
    : input.cards.filter((c) => !isClosed(c.status));
  const nodes = new Map((topology?.nodes ?? []).map((n) => [n.id, n]));
  const assigned = new Set<string>();
  for (const card of cards) {
    const executor = executorOf(card);
    if (card.assignee_bot_id) assigned.add(card.assignee_bot_id);
    if (executor) assigned.add(executor);
  }
  const roster = (
    input.onlyAssigned
      ? input.bots.filter((b) => assigned.has(b.id))
      : input.bots
  )
    .slice()
    .sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
  const nameCount = new Map<string, number>();
  for (const bot of roster)
    nameCount.set(bot.name, (nameCount.get(bot.name) ?? 0) + 1);

  const bots: MapBot[] = roster.map((bot, index) => {
    const mine = (status: CardStatus) =>
      cards.filter(
        (c) =>
          c.status === status &&
          (c.assignee_bot_id === bot.id || executorOf(c) === bot.id),
      );
    const running = mine("RUNNING");
    const node = bot.node_id ? nodes.get(bot.node_id) : undefined;
    let availability: Availability;
    let reason: string;
    if (!bot.enabled) {
      availability = "unavailable";
      reason = "助手已停用";
    } else if (bot.node_id && (input.topologyUnknown || !topology)) {
      availability = "unknown";
      reason = "设备信息待同步";
    } else if (node && node.status !== "ONLINE") {
      availability = "unavailable";
      reason = node.status === "DISABLED" ? "所在设备已停用" : "所在设备离线";
    } else if (topology && bot.node_id && !node) {
      availability = "unknown";
      reason = "找不到所在设备";
    } else if (stale) {
      availability = "unknown";
      reason = "状态待同步";
    } else if (Math.max(bot.running ?? 0, running.length) > 0) {
      availability = "working";
      reason = "正在处理任务";
    } else {
      availability = "idle";
      reason = "已就绪，当前没有执行中的工作";
    }
    return {
      id: bot.id,
      name: bot.name,
      shortId: bot.id.slice(0, 4),
      role: bot.role,
      engine: bot.engine,
      nodeName: node?.name || bot.node_name || "",
      availability,
      reason,
      running,
      blocked: mine("BLOCKED"),
      review: mine("REVIEW"),
      runningCount: Math.max(bot.running ?? 0, running.length),
      // 座位只由排序后的 Bot ID 决定，状态变化不会让角色换位。
      zone: Math.floor(index / SEATS_PER_ZONE),
      seat: index % SEATS_PER_ZONE,
      nodeId: bot.node_id || "",
      // 设备信息缺失时不猜“本机”还是“远端”。
      nodeKind: nodeKindOf(node?.resource_type, !!node),
      model: bot.model ?? "",
      reasoningEffort: bot.reasoning_effort ?? "",
      concurrency: bot.concurrency ?? 1,
      skills: bot.skills ?? [],
      mcpServers: bot.mcp_servers ?? [],
      tags: bot.tags ?? [],
      finished: input.cards
        .filter((c) => isClosed(c.status) && c.last_job?.bot_id === bot.id)
        .sort((a, b) => stamp(b.updated_at) - stamp(a.updated_at))
        .slice(0, 3),
    };
  });

  const groups: Record<BoardGroupKey, Card[]> = {
    draft: [],
    ready: [],
    running: [],
    attention: [],
    closed: [],
  };
  for (const card of cards)
    for (const group of boardGroups)
      if (group.statuses.includes(card.status)) groups[group.key].push(card);

  const seen = new Map<string, MapEvent>();
  for (const card of cards) {
    // 真实事件（含评论与 @提及）按后端事件 ID 去重。
    for (const e of card.recent_events ?? []) {
      const key = `${card.id}#${e.id}`;
      seen.set(key, {
        key,
        cardId: card.id,
        cardTitle: card.title,
        kind: e.kind,
        body: e.body,
        at: e.at,
        actor: e.actor,
        mentions: e.mentions,
      });
    }
    if (!card.latest) continue;
    // latest 的进度/状态/提问已包含在 recent_events 里；旧后端没有时才回退，引擎活动提示没有事件 ID。
    if (card.recent_events?.length && card.latest.kind !== "activity") continue;
    const key = `${card.id}:${card.latest.kind}:${card.latest.at}`;
    seen.set(key, {
      key,
      cardId: card.id,
      cardTitle: card.title,
      kind: card.latest.kind,
      body: card.latest.body,
      at: card.latest.at,
    });
  }
  const events = [...seen.values()]
    .sort((a, b) => stamp(b.at) - stamp(a.at))
    .slice(0, 50);

  const summary = { working: 0, idle: 0, unavailable: 0, unknown: 0 };
  for (const bot of bots) summary[bot.availability] += 1;
  return {
    bots,
    zones: Math.max(1, Math.ceil(bots.length / SEATS_PER_ZONE)),
    groups,
    delivered: groups.closed.filter((c) => c.status === "DONE" && c.summary),
    events,
    summary,
  };
}

/**
 * 快照后只播放没见过的新事件：首次调用只记录不播放；乱序或重复到达的同一事件最多一次。
 */
export function createEventTracker() {
  const seen = new Set<string>();
  let primed = false;
  return {
    reset() {
      seen.clear();
      primed = false;
    },
    fresh(events: MapEvent[]): MapEvent[] {
      const unseen = events.filter((e) => !seen.has(e.key));
      for (const e of events) seen.add(e.key);
      if (!primed) {
        primed = true;
        return [];
      }
      return unseen;
    },
  };
}

/** 事件突发时只保留问题、失败和完成类，其余按时间合并，最多返回 limit 条。 */
export function pickBubbles(events: MapEvent[], limit = 2): MapEvent[] {
  const priority = (e: MapEvent) =>
    e.kind === "question"
      ? 0
      : e.kind === "result" || e.kind === "status"
        ? 1
        : 2;
  const merged = new Map<string, MapEvent>();
  for (const e of events) {
    const current = merged.get(e.cardId);
    if (
      !current ||
      priority(e) < priority(current) ||
      stamp(e.at) > stamp(current.at)
    )
      merged.set(e.cardId, e);
  }
  return [...merged.values()]
    .sort((a, b) => priority(a) - priority(b) || stamp(b.at) - stamp(a.at))
    .slice(0, limit);
}

export const hiddenText = "工作内容已隐藏";
/** 气泡和动态只摘录简短纯文本；过长的截断并保留“查看原文”。 */
export function excerpt(text: string, max = 60) {
  const clean = text.replace(/\s+/g, " ").trim();
  return {
    text: clean.length > max ? clean.slice(0, max - 1) + "…" : clean,
    cut: clean.length > max,
  };
}
