export interface GraphStage {
  key: string;
  agent_id: string;
  work_item_id: string;
  depends_on: string[];
}
export interface GraphItem {
  id: string;
  status: string;
  goal: string;
  assignee_agent_id: string;
  conversation_id: string;
  remote_jobs?: { id: string; node_id: string | null; status: string }[];
}
export interface GraphNode {
  key: string;
  agentId: string;
  layer: number;
  row: number;
  dependsOn: string[];
  item?: GraphItem;
}

const NODE_W = 188;
const NODE_H = 92;
const GAP_X = 64;
const GAP_Y = 22;
const PAD = 12;

const STATUS_TEXT: Record<string, string> = {
  QUEUED: "排队中",
  RUNNING: "执行中",
  WAITING_BOT: "等待上游",
  WAITING_HUMAN: "等待人工",
  COMPLETED: "已完成",
  FAILED: "失败",
  CANCELLED: "已取消",
};
const STATUS_TONE: Record<string, string> = {
  QUEUED: "idle",
  RUNNING: "run",
  WAITING_BOT: "wait",
  WAITING_HUMAN: "warn",
  COMPLETED: "ok",
  FAILED: "bad",
  CANCELLED: "off",
};

/** 按最长依赖路径分层；同层按阶段声明顺序排行。依赖指向未知阶段时忽略。 */
export function layoutStages(
  stages: GraphStage[],
  items: GraphItem[],
): GraphNode[] {
  const byKey = new Map(stages.map((s) => [s.key, s]));
  const layers = new Map<string, number>();
  function depth(key: string, trail: Set<string>): number {
    const cached = layers.get(key);
    if (cached !== undefined) return cached;
    if (trail.has(key)) return 0;
    trail.add(key);
    const deps = (byKey.get(key)?.depends_on ?? []).filter((d) => byKey.has(d));
    const value = deps.length
      ? Math.max(...deps.map((d) => depth(d, trail))) + 1
      : 0;
    trail.delete(key);
    layers.set(key, value);
    return value;
  }
  const rows = new Map<number, number>();
  return stages.map((stage) => {
    const layer = depth(stage.key, new Set());
    const row = rows.get(layer) ?? 0;
    rows.set(layer, row + 1);
    return {
      key: stage.key,
      agentId: stage.agent_id,
      layer,
      row,
      dependsOn: stage.depends_on.filter((d) => byKey.has(d)),
      item: items.find((i) => i.id === stage.work_item_id),
    };
  });
}

export function WorkflowGraph({
  stages,
  items,
  botName,
  nodeLabel,
  onOpen,
}: {
  stages: GraphStage[];
  items: GraphItem[];
  botName: (agentId: string) => string;
  /** 阶段所在资源：优先显示实际派发的远程节点，其次 Bot 固定绑定的节点。 */
  nodeLabel: (agentId: string, nodeId: string | null | undefined) => string;
  onOpen?: (conversationId: string) => void;
}) {
  const nodes = layoutStages(stages, items);
  const layerCount = Math.max(...nodes.map((n) => n.layer), 0) + 1;
  const rowCount = Math.max(...nodes.map((n) => n.row), 0) + 1;
  const width = PAD * 2 + layerCount * NODE_W + (layerCount - 1) * GAP_X;
  const height = PAD * 2 + rowCount * NODE_H + (rowCount - 1) * GAP_Y;
  const pos = (n: GraphNode) => ({
    x: PAD + n.layer * (NODE_W + GAP_X),
    y: PAD + n.row * (NODE_H + GAP_Y),
  });
  const byKey = new Map(nodes.map((n) => [n.key, n]));
  const done = nodes.filter((n) => n.item?.status === "COMPLETED").length;
  const edges = nodes.flatMap((n) =>
    n.dependsOn.map((from) => ({ from: byKey.get(from)!, to: n })),
  );

  return (
    <div className="wfg" aria-label="流程图">
      <div className="wfg-head">
        <span>
          完成 {done} / {nodes.length} 个阶段
        </span>
        <span className="wfg-bar" aria-hidden>
          <i
            style={{ width: `${(done / Math.max(nodes.length, 1)) * 100}%` }}
          />
        </span>
      </div>
      <div className="wfg-scroll">
        <div className="wfg-canvas" style={{ width, height }}>
          <svg width={width} height={height} aria-hidden>
            <defs>
              <marker
                id="wfg-arrow"
                viewBox="0 0 8 8"
                refX="7"
                refY="4"
                markerWidth="7"
                markerHeight="7"
                orient="auto"
              >
                <path d="M0 0 L8 4 L0 8 z" fill="currentColor" />
              </marker>
            </defs>
            {edges.map(({ from, to }) => {
              const a = pos(from);
              const b = pos(to);
              const x1 = a.x + NODE_W;
              const y1 = a.y + NODE_H / 2;
              const x2 = b.x;
              const y2 = b.y + NODE_H / 2;
              const mid = (x1 + x2) / 2;
              const tone = STATUS_TONE[from.item?.status ?? ""] ?? "idle";
              return (
                <path
                  key={`${from.key}>${to.key}`}
                  className={`wfg-edge ${tone === "ok" ? "passed" : ""} ${tone === "bad" || tone === "off" ? "broken" : ""}`}
                  d={`M${x1} ${y1} C${mid} ${y1} ${mid} ${y2} ${x2 - 2} ${y2}`}
                  markerEnd="url(#wfg-arrow)"
                />
              );
            })}
          </svg>
          {nodes.map((n) => {
            const { x, y } = pos(n);
            const status = n.item?.status ?? "";
            const tone = STATUS_TONE[status] ?? "idle";
            const job = n.item?.remote_jobs?.[0];
            const where = nodeLabel(n.agentId, job?.node_id);
            return (
              <button
                type="button"
                key={n.key}
                className={`wfg-node ${tone}`}
                style={{ left: x, top: y, width: NODE_W, height: NODE_H }}
                disabled={!n.item || !onOpen}
                title={n.item?.goal}
                onClick={() => n.item && onOpen?.(n.item.conversation_id)}
              >
                <span className="wfg-key">{n.key}</span>
                <span className="wfg-bot">{botName(n.agentId)}</span>
                <span className="wfg-where">{where}</span>
                <span className={`wfg-status ${tone}`}>
                  {STATUS_TEXT[status] ?? (status || "未创建")}
                  {job ? ` · 远程 ${job.status}` : ""}
                </span>
              </button>
            );
          })}
        </div>
      </div>
    </div>
  );
}
