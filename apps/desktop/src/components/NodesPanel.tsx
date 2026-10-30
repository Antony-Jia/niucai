import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Monitor, Pencil, Plus, Power, Trash2 } from "lucide-react";
import { native, openNodePreview, request } from "../lib/api";
import { boardApi, type Bot } from "../lib/board";
import { useApp, useData } from "../lib/state";
import { BotForm } from "./BotForm";
import { LocalNode } from "./LocalNode";
import { Modal, time } from "./ui";

/** 节点心跳上报的各引擎可选模型。 */
export const engineModels = (
  engines: Record<string, { models?: string[] }>,
): Record<string, string[]> =>
  Object.fromEntries(
    Object.entries(engines).map(([name, e]) => [name, e.models ?? []]),
  );

/** 节点心跳上报的各引擎可选推理强度。 */
export const engineEfforts = (
  engines: Record<string, { efforts?: string[] }>,
): Record<string, string[]> =>
  Object.fromEntries(
    Object.entries(engines).map(([name, e]) => [name, e.efforts ?? []]),
  );

export interface TopologyNode {
  id: string;
  name: string;
  resource_type: string;
  platform: string;
  runner_version: string;
  status: "ONLINE" | "OFFLINE" | "DISABLED";
  enabled: boolean;
  slots: number;
  busy: number;
  engines: Record<
    string,
    { available?: boolean; models?: string[]; efforts?: string[] }
  >;
  preview_url: string;
  paired_at: string | null;
  heartbeat_at: string | null;
  bots: {
    id: string;
    name: string;
    engine: string;
    role: string;
    enabled: boolean;
    running: number;
    concurrency: number;
  }[];
}
export interface Topology {
  kernel: { instance_id: string; remote_enabled: boolean };
  nodes: TopologyNode[];
}
interface Pairing {
  code: string;
  expires_at: string;
  name: string;
}

const statusText = { ONLINE: "在线", OFFLINE: "离线", DISABLED: "已停用" };
const typeText: Record<string, string> = {
  local_docker: "本地 Docker",
  remote_vps: "VPS",
  windows_local: "本机 Windows",
  unspecified: "节点",
};

/** Docker 容器内访问宿主机 Kernel 需改用 host.docker.internal。 */
export function nodeKernelUrl(baseUrl: string, type: string) {
  if (type !== "local_docker") return baseUrl;
  return baseUrl.replace(
    /^(https?:\/\/)(127\.0\.0\.1|localhost)(?=[:/]|$)/,
    "$1host.docker.internal",
  );
}

export function runCommand(kernelUrl: string, code: string) {
  return [
    "docker build -f services/node-runner/Dockerfile -t niucai-node .",
    "docker run -d --name niucai-node --restart unless-stopped " +
      `-e NIUCAI_KERNEL_URL=${kernelUrl} -e NIUCAI_PAIRING_CODE=${code} ` +
      "-e NIUCAI_ENGINES=pi -e NIUCAI_PI_MODEL=<模型名> -e NIUCAI_PI_PROVIDER_KEY " +
      "-v niucai-node-data:/data niucai-node",
  ].join("\n");
}

/** 放射状小拓扑：Kernel 居中，节点环绕，节点上的 Bot 以小点挂在外侧。 */
export function TopologyMap({ topology }: { topology: Topology }) {
  const size = 320;
  const c = size / 2;
  const r = 112;
  const nodes = topology.nodes;
  return (
    <svg
      className="topology"
      viewBox={`0 0 ${size} ${size}`}
      role="img"
      aria-label={`拓扑：Kernel 与 ${nodes.length} 个节点`}
    >
      {nodes.map((node, i) => {
        const a = (2 * Math.PI * i) / nodes.length - Math.PI / 2;
        const x = c + r * Math.cos(a);
        const y = c + r * Math.sin(a);
        return (
          <g
            key={node.id}
            className={`topology-node ${node.status.toLowerCase()}`}
          >
            <line x1={c} y1={c} x2={x} y2={y} />
            {node.bots.map((bot, j) => {
              // Bot 沿节点外侧的小弧排开，最多画 6 个，其余在节点列表里看。
              if (j >= 6) return null;
              const b =
                a + (j - (Math.min(node.bots.length, 6) - 1) / 2) * 0.45;
              const bx = x + 34 * Math.cos(b);
              const by = y + 34 * Math.sin(b);
              return (
                <g
                  key={bot.id}
                  className={`topology-bot ${bot.running ? "busy" : ""} ${bot.enabled ? "" : "disabled"}`}
                >
                  <line x1={x} y1={y} x2={bx} y2={by} />
                  <circle cx={bx} cy={by} r={6}>
                    <title>
                      @{bot.name} · {bot.engine}
                      {bot.running ? ` · 执行中 ${bot.running}` : ""}
                    </title>
                  </circle>
                </g>
              );
            })}
            <circle cx={x} cy={y} r={22} />
            <text x={x} y={y + 38} textAnchor="middle">
              {node.name.length > 10 ? node.name.slice(0, 9) + "…" : node.name}
            </text>
            <text
              x={x}
              y={y + 4}
              textAnchor="middle"
              className="topology-slots"
            >
              {node.busy}/{node.slots}
            </text>
          </g>
        );
      })}
      <g className="topology-kernel">
        <circle cx={c} cy={c} r={30} />
        <text x={c} y={c + 4} textAnchor="middle">
          Kernel
        </text>
      </g>
    </svg>
  );
}

function AddNode({ onClose }: { onClose: () => void }) {
  const { profile, notify } = useApp();
  const [name, setName] = useState("");
  const [type, setType] = useState("remote_vps");
  const [kernelUrl, setKernelUrl] = useState(profile?.base_url ?? "");
  const [pairing, setPairing] = useState<Pairing | null>(null);
  const [busy, setBusy] = useState(false);
  const [known, setKnown] = useState<Set<string>>(new Set());
  // 等待配对期间加快轮询，节点上线即提示。
  const topology = useQuery({
    queryKey: ["topology"],
    queryFn: () => request<Topology>("/api/topology"),
    refetchInterval: pairing ? 3000 : false,
  });
  const paired = pairing
    ? topology.data?.nodes.find(
        (n) => !known.has(n.id) && n.status === "ONLINE",
      )
    : undefined;
  async function create() {
    setBusy(true);
    try {
      setKnown(new Set(topology.data?.nodes.map((n) => n.id)));
      setPairing(
        await request<Pairing>("/api/pairing", "POST", {
          name: name.trim(),
          resource_type: type,
        }),
      );
    } catch (e) {
      notify(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal title="添加节点" onClose={onClose}>
      {!pairing ? (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void create();
          }}
        >
          <label>
            节点名称
            <input
              required
              maxLength={200}
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="例如：VPS-东京"
            />
          </label>
          <label>
            类型
            <select value={type} onChange={(e) => setType(e.target.value)}>
              <option value="remote_vps">VPS（Docker）</option>
              <option value="local_docker">本机 Docker</option>
            </select>
          </label>
          <label>
            节点可访问的 Kernel 地址
            <input
              type="url"
              required
              value={kernelUrl}
              onChange={(e) => setKernelUrl(e.target.value)}
              autoCapitalize="none"
              spellCheck={false}
            />
            <small>节点只主动连接 Kernel，不需要开放入站端口。</small>
          </label>
          <div className="button-row">
            <button className="primary" disabled={busy || !name.trim()}>
              {busy ? "正在生成…" : "生成配对码"}
            </button>
          </div>
        </form>
      ) : (
        <div className="pairing">
          <p>
            配对码（10 分钟内有效，仅可使用一次）：
            <strong className="pairing-code">{pairing.code}</strong>
          </p>
          <p className="muted">
            在仓库根目录执行；先在当前 shell 设置
            NIUCAI_PI_PROVIDER_KEY，密钥不写进命令行。
          </p>
          <pre className="pairing-command">
            {runCommand(nodeKernelUrl(kernelUrl, type), pairing.code)}
          </pre>
          <p role="status">
            {paired ? (
              <span className="badge online">已上线：{paired.name}</span>
            ) : (
              <span className="muted">等待节点上线…</span>
            )}
          </p>
          <div className="button-row">
            <button onClick={onClose}>{paired ? "完成" : "关闭"}</button>
          </div>
        </div>
      )}
    </Modal>
  );
}

export function NodesPanel() {
  const { notify, demo, synchronizing } = useApp();
  const cache = useQueryClient();
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState<{
    node: TopologyNode;
    bot?: Bot;
  } | null>(null);
  const topology = useData(["topology"], () =>
    request<Topology>("/api/topology"),
  );
  const bots = useData(["bots"], boardApi.bots);
  async function act(action: () => Promise<unknown>) {
    if (synchronizing) {
      notify("请等待 Kernel 恢复连接后再操作节点");
      return;
    }
    try {
      await action();
      await Promise.all([
        cache.invalidateQueries({ queryKey: ["topology"] }),
        cache.invalidateQueries({ queryKey: ["bots"] }),
      ]);
    } catch (e) {
      notify(e instanceof Error ? e.message : String(e));
    }
  }
  const data = topology.data;
  const remoteOff = data && !data.kernel.remote_enabled;
  const refresh = () =>
    void cache.invalidateQueries({ queryKey: ["topology"] });
  return (
    <div className="nodes-layout">
      <div className="nodes-side">
        <section className="panel">
          <div className="section-heading">
            <h2>拓扑</h2>
            <button
              className="primary"
              disabled={!data || remoteOff || demo}
              onClick={() => setAdding(true)}
              aria-label="添加节点"
            >
              <Plus size={15} />
              添加节点
            </button>
          </div>
          {remoteOff && (
            <p className="muted">
              Kernel 未启用远程节点（NIUCAI_REMOTE_PI_ENABLED）。
            </p>
          )}
          {data && <TopologyMap topology={data} />}
          {data && data.nodes.length === 0 && (
            <p className="muted">无可用bot：还没有节点，点击“添加节点”。</p>
          )}
          {data &&
            data.nodes.length > 0 &&
            data.nodes.every((n) => n.bots.length === 0) && (
              <p className="muted">无可用bot：在节点下点击“添加 Bot”。</p>
            )}
        </section>
        {native() && !demo && !remoteOff && <LocalNode onChange={refresh} />}
      </div>
      <section className="panel">
        <h2>节点</h2>
        {data?.nodes.map((node) => (
          <article key={node.id} className="node-row">
            <div>
              <strong>{node.name}</strong>
              <span className={`badge ${node.status.toLowerCase()}`}>
                {statusText[node.status]}
              </span>
              <p className="muted">
                {typeText[node.resource_type] ?? "节点"}
                {node.platform && ` · ${node.platform}`}
                {` · 引擎 ${Object.keys(node.engines).join("/") || "未上报"}`}
                {node.heartbeat_at && ` · 心跳 ${time(node.heartbeat_at)}`}
              </p>
              {node.preview_url &&
                (native() ? (
                  <button
                    disabled={demo || node.status === "OFFLINE"}
                    title={
                      node.status === "OFFLINE"
                        ? "节点离线，暂时无法查看画面"
                        : "在独立窗口里查看这台节点上的浏览器（VNC 密码在画面页里输入）"
                    }
                    onClick={() =>
                      void openNodePreview(node.id).catch((e) =>
                        notify(e instanceof Error ? e.message : String(e)),
                      )
                    }
                  >
                    <Monitor size={13} /> 查看远程画面
                  </button>
                ) : (
                  <a href={node.preview_url} target="_blank" rel="noreferrer">
                    查看远程画面
                  </a>
                ))}
              <ul className="node-bots" aria-label={`${node.name} 上的 Bot`}>
                {node.bots.map((summary) => {
                  const bot = bots.data?.find((b) => b.id === summary.id);
                  return (
                    <li key={summary.id}>
                      <span>
                        @{summary.name} · {summary.engine}
                        {summary.role && ` · ${summary.role}`}
                        {` · ${summary.running}/${summary.concurrency}`}
                        {!summary.enabled && " · 已停用"}
                      </span>
                      <button
                        className="icon-button"
                        aria-label={`编辑 ${summary.name}`}
                        disabled={demo || !bot}
                        onClick={() => setEditing({ node, bot })}
                      >
                        <Pencil size={13} />
                      </button>
                      <button
                        className="icon-button"
                        aria-label={`${summary.enabled ? "停用" : "启用"} ${summary.name}`}
                        disabled={demo}
                        onClick={() =>
                          void act(() =>
                            boardApi.updateBot(summary.id, {
                              enabled: !summary.enabled,
                            }),
                          )
                        }
                      >
                        <Power size={13} />
                      </button>
                      <button
                        className="icon-button"
                        aria-label={`删除 ${summary.name}`}
                        disabled={demo}
                        onClick={() => {
                          if (confirm(`删除 Bot @${summary.name}？`))
                            void act(() => boardApi.deleteBot(summary.id));
                        }}
                      >
                        <Trash2 size={13} />
                      </button>
                    </li>
                  );
                })}
              </ul>
              <button
                disabled={demo || !node.enabled}
                onClick={() => setEditing({ node })}
              >
                <Plus size={14} />
                添加 Bot
              </button>
            </div>
            <div className="button-row">
              <label className="slots">
                槽位
                <input
                  type="number"
                  min={1}
                  max={16}
                  value={node.slots}
                  disabled={demo}
                  onChange={(e) =>
                    void act(() =>
                      request(`/api/remote/nodes/${node.id}`, "PATCH", {
                        slots: Math.min(
                          16,
                          Math.max(1, Number(e.target.value)),
                        ),
                      }),
                    )
                  }
                />
              </label>
              <button
                disabled={demo}
                onClick={() =>
                  void act(() =>
                    request(`/api/remote/nodes/${node.id}`, "PATCH", {
                      enabled: !node.enabled,
                    }),
                  )
                }
              >
                <Power size={14} />
                {node.enabled ? "停用" : "启用"}
              </button>
              <button
                disabled={demo}
                onClick={() => {
                  if (
                    confirm(
                      `吊销节点“${node.name}”的凭据？需重新配对才能使用。`,
                    )
                  )
                    void act(() =>
                      request(`/api/remote/nodes/${node.id}/revoke`, "POST"),
                    );
                }}
              >
                <Trash2 size={14} />
                吊销
              </button>
            </div>
          </article>
        ))}
      </section>
      {editing && (
        <BotForm
          nodeId={editing.node.id}
          engines={Object.entries(editing.node.engines)
            .filter(([, e]) => e.available !== false)
            .map(([name]) => name)}
          models={engineModels(editing.node.engines)}
          efforts={engineEfforts(editing.node.engines)}
          bot={editing.bot}
          platform={editing.node.platform}
          onClose={() => setEditing(null)}
        />
      )}
      {adding && (
        <AddNode
          onClose={() => {
            setAdding(false);
            void cache.invalidateQueries({ queryKey: ["topology"] });
          }}
        />
      )}
    </div>
  );
}
