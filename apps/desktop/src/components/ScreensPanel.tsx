import { useState } from "react";
import { Monitor, Plus } from "lucide-react";
import { api, native, openNodePreview, request } from "../lib/api";
import { useApp, useData } from "../lib/state";
import { ComputerPage, RegisterComputerModal } from "../pages/Computer";
import type { Topology, TopologyNode } from "./NodesPanel";
import { Badge, ConnectionEmpty, time } from "./ui";

interface Item {
  key: string;
  name: string;
  status: string;
  /** computer：Kernel 登记的可接管桌面；node：节点上报的预览画面。 */
  kind: "computer" | "node";
  hint: string;
  node?: TopologyNode;
}

const typeText: Record<string, string> = {
  local_docker: "本地 Docker",
  remote_vps: "VPS",
  windows_local: "本机 Windows",
  unspecified: "节点",
};

function NodeScreen({ node }: { node: TopologyNode }) {
  const { demo, notify } = useApp();
  const offline = node.status === "OFFLINE";
  const open = () =>
    void openNodePreview(node.id).catch((e) =>
      notify(e instanceof Error ? e.message : String(e)),
    );
  return (
    <section className="panel node-screen">
      <div className="section-heading">
        <h2>{node.name}</h2>
        <Badge status={node.status} />
      </div>
      <p className="muted">
        {typeText[node.resource_type] ?? "节点"}
        {node.platform && ` · ${node.platform}`}
        {node.heartbeat_at && ` · 心跳 ${time(node.heartbeat_at)}`}
      </p>
      <div className="remote-placeholder">
        <div className="computer-illustration large">
          <Monitor size={68} />
        </div>
        {node.preview_url ? (
          <>
            <h2>节点上的远程画面</h2>
            <p>
              在独立窗口里查看这台节点上的浏览器，口令在画面页里输入。仅查看，
              不涉及 Kernel 的接管。
            </p>
            {native() ? (
              <button
                className="primary"
                disabled={demo || offline}
                title={offline ? "节点离线，暂时无法查看画面" : undefined}
                onClick={open}
              >
                <Monitor size={16} />
                打开远程画面
              </button>
            ) : (
              <a
                className="screen-link"
                href={node.preview_url}
                target="_blank"
                rel="noreferrer"
              >
                打开远程画面
              </a>
            )}
          </>
        ) : (
          <>
            <h2>该节点未提供预览</h2>
            <p>
              节点没有上报预览地址。在节点 runner 上设置 NIUCAI_PREVIEW_URL
              （HTTPS）后重启，下一次心跳就会出现。
            </p>
          </>
        )}
      </div>
      {node.bots.length > 0 && (
        <ul className="node-bots" aria-label={`${node.name} 上的 Bot`}>
          {node.bots.map((b) => (
            <li key={b.id}>
              <span>
                @{b.name} · {b.engine}
                {b.role && ` · ${b.role}`}
                {` · ${b.running}/${b.concurrency}`}
                {!b.enabled && " · 已停用"}
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

/**
 * 远程画面：把可接管的电脑与节点上的预览画面放进同一个可切换的列表。
 * 只有 Kernel 登记的电脑能接管；节点画面仅供查看，两者在列表里用标签区分。
 */
export function ScreensPanel() {
  const { connected } = useApp();
  const computers = useData(["computers"], api.computers);
  const topology = useData(["topology"], () =>
    request<Topology>("/api/topology"),
  );
  const [selected, setSelected] = useState("");
  const [register, setRegister] = useState(false);
  if (!connected) return <ConnectionEmpty />;
  const nodes = [...(topology.data?.nodes ?? [])].sort(
    (a, b) => Number(!!b.preview_url) - Number(!!a.preview_url),
  );
  const items: Item[] = [
    ...(computers.data ?? []).map((c) => ({
      key: `computer:${c.id}`,
      name: c.name,
      status: c.status,
      kind: "computer" as const,
      hint: "可接管",
    })),
    ...nodes.map((n) => ({
      key: `node:${n.id}`,
      name: n.name,
      status: n.status,
      kind: "node" as const,
      hint: n.preview_url ? "仅查看" : "无预览",
      node: n,
    })),
  ];
  const current = items.find((i) => i.key === selected) ?? items[0];
  return (
    <div className="nodes-layout">
      <section className="panel">
        <div className="section-heading">
          <h2>画面</h2>
          <button onClick={() => setRegister(true)} aria-label="注册电脑">
            <Plus size={15} />
            注册电脑
          </button>
        </div>
        {items.length === 0 ? (
          <p className="muted">
            还没有电脑或节点。注册一台电脑，或在“执行环境”里添加节点。
          </p>
        ) : (
          <ul className="screen-list" aria-label="远程画面列表">
            {items.map((item) => (
              <li key={item.key}>
                <button
                  className={item === current ? "active" : ""}
                  aria-pressed={item === current}
                  onClick={() => setSelected(item.key)}
                >
                  <Monitor size={15} />
                  <span className="screen-name">{item.name}</span>
                  <span className={`screen-tag ${item.kind}`}>{item.hint}</span>
                  <Badge status={item.status} />
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>
      <div>
        {current?.kind === "computer" && (
          <ComputerPage
            embedded
            computerId={current.key.slice("computer:".length)}
          />
        )}
        {current?.kind === "node" && current.node && (
          <NodeScreen node={current.node} />
        )}
      </div>
      {register && (
        <RegisterComputerModal
          onClose={() => setRegister(false)}
          onCreated={(id) => setSelected(`computer:${id}`)}
        />
      )}
    </div>
  );
}
