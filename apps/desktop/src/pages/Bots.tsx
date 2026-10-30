import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Bot as BotIcon, Plus, Search, Pencil, Power } from "lucide-react";
import {
  Header,
  Empty,
  Loading,
  ErrorNotice,
  Badge,
  Modal,
} from "../components/ui";
import { BotForm } from "../components/BotForm";
import {
  engineEfforts,
  engineModels,
  type Topology,
} from "../components/NodesPanel";
import { boardApi, type Bot as NodeBot } from "../lib/board";
import { request } from "../lib/api";
import { useApp } from "../lib/state";
import { LegacyBots } from "./LegacyBots";

export function Bots({
  onTask,
}: { onTask?: (id: string | null) => void } = {}) {
  const { connected, demo, synchronizing, setPage, notify } = useApp();
  const cache = useQueryClient();
  const [tab, setTab] = useState("bots");
  const [search, setSearch] = useState("");
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<NodeBot | undefined>();
  const [nodeId, setNodeId] = useState("");
  const [busy, setBusy] = useState(false);
  const enabled = connected && !demo;
  const topology = useQuery({
    queryKey: ["topology"],
    queryFn: () => request<Topology>("/api/topology"),
    enabled,
    refetchInterval: 5000,
  });
  const bots = useQuery({
    queryKey: ["bots"],
    queryFn: boardApi.bots,
    enabled,
    refetchInterval: 5000,
  });
  const nodes = topology.data?.nodes ?? [];
  const node = nodes.find((n) => n.id === nodeId);
  const ready = enabled && !synchronizing && !topology.isError && !bots.isError;
  const list =
    bots.data?.filter((b) =>
      `${b.name} ${b.role} ${b.tags.join(" ")}`
        .toLowerCase()
        .includes(search.toLowerCase()),
    ) ?? [];
  function create() {
    setEditing(undefined);
    setNodeId("");
    setCreating(true);
  }
  async function toggle(bot: NodeBot) {
    if (!ready || busy) return;
    setBusy(true);
    try {
      await boardApi.updateBot(bot.id, { enabled: !bot.enabled });
      await cache.invalidateQueries({ queryKey: ["bots"] });
      await cache.invalidateQueries({ queryKey: ["topology"] });
    } catch (e) {
      notify(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="bots-page">
      <Header
        eyebrow="YOUR TEAM"
        title="Bots"
        description="为每项工作找到合适的角色，在你的节点上执行。"
      >
        <button onClick={() => setPage("settings")}>管理节点</button>
        <button className="primary" onClick={create}>
          <Plus size={16} />
          新建 Bot
        </button>
      </Header>
      <div className="settings-tabs" role="tablist" aria-label="Bot 管理方式">
        <button
          role="tab"
          aria-selected={tab === "bots"}
          onClick={() => setTab("bots")}
        >
          节点 Bots
        </button>
        <button
          role="tab"
          aria-selected={tab === "legacy"}
          onClick={() => setTab("legacy")}
        >
          旧版协作
        </button>
      </div>
      {tab === "legacy" ? (
        <>
          <p className="muted">
            旧版角色用于历史协作流程。白板与 Goal 使用“节点 Bots”。
          </p>
          <LegacyBots onTask={onTask} />
        </>
      ) : (
        <>
          <div className="bot-overview">
            <div>
              <strong>{bots.data?.length ?? "—"}</strong>
              <span>团队成员</span>
            </div>
            <div>
              <strong>
                {topology.data
                  ? nodes.filter((n) => n.status === "ONLINE").length
                  : "—"}
              </strong>
              <span>在线节点{!ready ? " · 状态待确认" : ""}</span>
            </div>
            <div>
              <strong>
                {bots.data
                  ? bots.data.reduce((sum, b) => sum + b.running, 0)
                  : "—"}
              </strong>
              <span>正在执行</span>
            </div>
          </div>
          <ErrorNotice error={topology.error || bots.error} />
          {!enabled && (
            <p className="connection-help">
              连接真实 Kernel 后可加载成员与执行资源。你可以先了解创建步骤。
              <button onClick={() => setPage("settings")}>配置连接</button>
            </p>
          )}
          <div className="bot-toolbar">
            <h2>团队成员</h2>
            <label className="bot-search">
              <Search size={16} />
              <input
                aria-label="搜索 Bot"
                placeholder="搜索名称、角色或标签"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
            </label>
          </div>
          {enabled && bots.isLoading ? (
            <Loading />
          ) : bots.isError && !bots.data ? (
            <Empty
              title="暂时无法加载 Bots"
              description="检查顶部的 Kernel 连接，恢复后会自动刷新。"
            />
          ) : list.length ? (
            <div className="bot-grid">
              {list.map((bot) => {
                const host = nodes.find((n) => n.id === bot.node_id);
                return (
                  <article className="panel bot-card" key={bot.id}>
                    <div className="section-heading">
                      <span className="bot-avatar">
                        <BotIcon size={22} />
                      </span>
                      <Badge
                        status={
                          !bot.enabled
                            ? "DISABLED"
                            : !ready
                              ? "待同步"
                              : (host?.status ?? "OFFLINE")
                        }
                      />
                    </div>
                    <h3>@{bot.name}</h3>
                    <p>{bot.role || "通用执行助手"}</p>
                    <p className="muted">
                      {host?.name || bot.node_name || "节点待确认"} ·{" "}
                      {bot.engine}
                    </p>
                    <div className="bot-tags">
                      {bot.tags.map((t) => (
                        <span key={t}>{t}</span>
                      ))}
                    </div>
                    <div className="bot-card-footer">
                      <span>
                        {bot.running
                          ? `${bot.running} 项执行中`
                          : bot.enabled
                            ? "等待卡片"
                            : "已停用"}
                      </span>
                      <div className="button-row">
                        <button
                          aria-label={`编辑 ${bot.name}`}
                          disabled={!ready}
                          onClick={() => {
                            setEditing(bot);
                            setNodeId(bot.node_id);
                            setCreating(true);
                          }}
                        >
                          <Pencil size={14} />
                          编辑
                        </button>
                        <button
                          aria-label={`${bot.enabled ? "停用" : "启用"} ${bot.name}`}
                          disabled={!ready || busy}
                          onClick={() => void toggle(bot)}
                        >
                          <Power size={14} />
                          {bot.enabled ? "停用" : "启用"}
                        </button>
                      </div>
                    </div>
                  </article>
                );
              })}
            </div>
          ) : (
            <Empty
              title={search ? "没有匹配的 Bot" : "创建你的第一位 Bot"}
              description={
                search
                  ? "试试其他名称、角色或标签。"
                  : "选择执行节点，设置角色，再把工作交给白板。"
              }
            >
              {!search && (
                <button className="primary" onClick={create}>
                  新建 Bot
                </button>
              )}
            </Empty>
          )}
          {creating && !node && (
            <Modal title="选择执行节点" onClose={() => setCreating(false)}>
              <div className="bot-node-picker">
                <div className="section-heading">
                  <div>
                    <small className="eyebrow">STEP 01 / 02</small>
                    <h2>Bot 在哪里工作？</h2>
                  </div>
                  <button onClick={() => setCreating(false)}>取消</button>
                </div>
                <p className="muted">
                  选择已配对的执行节点。只显示节点实际上报的引擎。
                </p>
                {!ready ? (
                  <div className="connection-help">
                    <strong>先恢复 Kernel 连接</strong>
                    <p>暂时无法确认节点状态，恢复连接后可继续创建。</p>
                    <button onClick={() => setPage("settings")}>
                      检查连接
                    </button>
                  </div>
                ) : nodes.length === 0 ? (
                  <Empty
                    title="还没有执行节点"
                    description="在设置中添加本机 Windows、Docker 或 VPS 节点，再创建 Bot。"
                  >
                    <button onClick={() => setPage("settings")}>
                      添加节点
                    </button>
                  </Empty>
                ) : (
                  nodes.map((n) => (
                    <button
                      className="node-choice"
                      key={n.id}
                      disabled={
                        n.status !== "ONLINE" ||
                        !n.enabled ||
                        !Object.values(n.engines).some((e) => e.available)
                      }
                      onClick={() => setNodeId(n.id)}
                    >
                      <span>
                        <strong>{n.name}</strong>
                        <small>
                          {Object.entries(n.engines)
                            .filter(([, e]) => e.available)
                            .map(([k]) => k)
                            .join(" / ") || "无可用引擎"}{" "}
                          · {n.busy}/{n.slots} 槽位使用中
                        </small>
                      </span>
                      <Badge status={n.status} />
                    </button>
                  ))
                )}
              </div>
            </Modal>
          )}
          {creating && node && (
            <BotForm
              key={editing?.id ?? node.id}
              nodeId={node.id}
              nodeName={node.name}
              engines={Object.entries(node.engines)
                .filter(([, e]) => e.available)
                .map(([k]) => k)}
              models={engineModels(node.engines)}
              efforts={engineEfforts(node.engines)}
              platform={node.platform}
              bot={editing}
              unavailable={!ready || node.status !== "ONLINE" || !node.enabled}
              onBack={editing ? undefined : () => setNodeId("")}
              onClose={() => setCreating(false)}
            />
          )}
          <section className="panel bot-next">
            <BotIcon size={24} />
            <div>
              <h3>从一个 Bot 到协作团队</h3>
              <p className="muted">
                在白板发布卡片，指定负责 Bot；也可以创建 Goal，让 Kernel
                推进工作。
              </p>
            </div>
            <button onClick={() => setPage("board")}>打开白板</button>
            <button onClick={() => setPage("goals")}>创建 Goal</button>
          </section>
        </>
      )}
    </div>
  );
}
