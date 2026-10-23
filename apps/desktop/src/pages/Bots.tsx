import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Header, ErrorNotice, ConnectionEmpty, Badge } from "../components/ui";
import { WorkflowGraph } from "../components/WorkflowGraph";
import { request } from "../lib/api";
import { useApp } from "../lib/state";

export interface Bot {
  id: string;
  name: string;
  definition: {
    label?: string;
    description?: string;
    instructions?: string;
    tools?: string[];
    allowed_nodes?: string[] | null;
    resource_policy?: { strategy?: string; fixed_node_id?: string | null };
    skills?: string[] | null;
    mcp_servers?: string[] | null;
    enabled?: boolean;
    archived?: boolean;
    [key: string]: unknown;
  };
}
interface StageRecord {
  key: string;
  agent_id: string;
  work_item_id: string;
  depends_on: string[];
}
interface Workflow {
  id: string;
  status: string;
  limits?: { stages?: StageRecord[] };
  owner_agent_id: string;
  handoff_count: number;
  model_turns: number;
}
interface Item {
  id: string;
  workflow_id: string;
  assignee_agent_id: string;
  status: string;
  goal: string;
  conversation_id: string;
  version: number;
  labels: string[];
  definition_snapshot?: { definition_version?: number };
  remote_jobs?: {
    id: string;
    node_id: string | null;
    status: string;
    wait_state?: { reason?: string };
  }[];
  artifacts?: { id: string; content_hash: string }[];
}
interface StageDraft {
  key: string;
  agent_id: string;
  goal: string;
  depends_on: string[];
  output_requirements: string;
}
function newStage(index: number, previous?: string): StageDraft {
  return {
    key: `step${index}`,
    agent_id: "",
    goal: "",
    depends_on: previous ? [previous] : [],
    output_requirements: "",
  };
}
/** 白名单输入：留空=全部，none=全部禁止，其余逗号分隔（支持 *）。 */
function listText(value: string[] | null | undefined) {
  if (value === null || value === undefined) return "";
  return value.length ? value.join(", ") : "none";
}
function parseList(text: string): string[] | null {
  const trimmed = text.trim();
  if (!trimmed) return null;
  if (trimmed === "none") return [];
  return trimmed
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}
interface Routine {
  id: string;
  agent_id: string;
  schedule: string;
  timezone: string;
  enabled: boolean;
  next_run_at: string;
}

export function Bots({
  onTask,
}: { onTask?: (id: string | null) => void } = {}) {
  const { connected, demo, notify, setPage } = useApp();
  const cache = useQueryClient();
  const enabled = connected && !demo;
  const status = useQuery({
    queryKey: ["collaboration-status"],
    queryFn: () =>
      request<{
        enabled: boolean;
        stopped: boolean;
        resource_release: string;
        unconfirmed_local_runs: string[];
        unconfirmed_remote_jobs: string[];
        legacy_untracked_runs?: string[];
      }>("/api/collaboration/status"),
    enabled,
    refetchInterval: 3000,
  });
  const bots = useQuery({
    queryKey: ["bots"],
    queryFn: () => request<Bot[]>("/api/agents"),
    enabled,
  });
  const nodes = useQuery({
    queryKey: ["bot-resource-nodes"],
    queryFn: () =>
      request<{ id: string; name: string; connection_status: string }[]>(
        "/api/remote/nodes",
      ),
    enabled,
  });
  const workflows = useQuery({
    queryKey: ["workflows"],
    queryFn: () => request<Workflow[]>("/api/workflows"),
    enabled,
    refetchInterval: 3000,
  });
  const items = useQuery({
    queryKey: ["work-items"],
    queryFn: () => request<Item[]>("/api/work-items"),
    enabled,
    refetchInterval: 3000,
  });
  const routines = useQuery({
    queryKey: ["routines"],
    queryFn: () => request<Routine[]>("/api/routines"),
    enabled,
    refetchInterval: 10000,
  });
  const [selected, setSelected] = useState<string | null>(null);
  const [id, setId] = useState("");
  const [name, setName] = useState("");
  const [instructions, setInstructions] = useState("");
  const [tools, setTools] = useState(
    "files.read, bot_list, bot_send, bot_wait, bot_reply, board_list",
  );
  const [busy, setBusy] = useState(false);
  const [advanced, setAdvanced] = useState("{}");
  const [goal, setGoal] = useState("");
  const [participants, setParticipants] = useState<string[]>([]);
  const [delivery, setDelivery] = useState("");
  const [routineBot, setRoutineBot] = useState("main");
  const [cron, setCron] = useState("0 9 * * *");
  const [timezone, setTimezone] = useState("Asia/Shanghai");
  const [prompt, setPrompt] = useState("");
  const [pipelineGoal, setPipelineGoal] = useState("");
  const [stages, setStages] = useState<StageDraft[]>([newStage(1)]);
  function changeStage(index: number, patch: Partial<StageDraft>) {
    setStages((current) =>
      current.map((stage, i) => {
        if (i === index) return { ...stage, ...patch };
        // 阶段改名时同步更新其他阶段的依赖引用。
        if (patch.key === undefined) return stage;
        const old = current[index].key;
        return {
          ...stage,
          depends_on: stage.depends_on.map((d) => (d === old ? patch.key! : d)),
        };
      }),
    );
  }
  function removeStage(index: number) {
    setStages((current) => {
      const key = current[index].key;
      return current
        .filter((_, i) => i !== index)
        .map((stage) => ({
          ...stage,
          depends_on: stage.depends_on.filter((d) => d !== key),
        }));
    });
  }
  function binding(bot: Bot) {
    const policy = bot.definition.resource_policy;
    const allowed = bot.definition.allowed_nodes;
    if (policy?.strategy === "fixed" && policy.fixed_node_id) {
      const node = nodes.data?.find((n) => n.id === policy.fixed_node_id);
      return `绑定节点：${node ? `${node.name}（${node.connection_status}）` : policy.fixed_node_id}`;
    }
    if (allowed === null || allowed === undefined) return "节点：兼容授权";
    if (!allowed.length) return "不使用远程节点";
    return `节点池：${allowed.length} 个节点`;
  }
  function setDefinitionField(key: string, value: unknown) {
    try {
      setAdvanced(
        JSON.stringify({ ...JSON.parse(advanced), [key]: value }, null, 2),
      );
    } catch {
      notify("高级配置不是有效 JSON，请先修正");
    }
  }
  function resourceConfig() {
    try {
      return JSON.parse(advanced) as {
        allowed_nodes?: string[] | null;
        resource_policy?: { strategy?: string; fixed_node_id?: string | null };
        skills?: string[] | null;
        mcp_servers?: string[] | null;
      };
    } catch {
      return {};
    }
  }
  function updateResource(strategy: string, fixed_node_id: string | null) {
    const old = JSON.parse(advanced);
    setAdvanced(
      JSON.stringify(
        {
          ...old,
          resource_policy: { ...old.resource_policy, strategy, fixed_node_id },
          ...(fixed_node_id
            ? {
                allowed_nodes: Array.from(
                  new Set([...(old.allowed_nodes ?? []), fixed_node_id]),
                ),
              }
            : {}),
        },
        null,
        2,
      ),
    );
  }
  async function act(action: () => Promise<unknown>) {
    setBusy(true);
    try {
      await action();
      await cache.invalidateQueries();
    } catch (error) {
      notify(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }
  function edit(bot?: Bot) {
    setSelected(bot?.id ?? null);
    setId(bot?.id ?? "");
    setName(bot?.name ?? "");
    setInstructions(bot?.definition.instructions ?? "");
    setTools(bot?.definition.tools?.join(", ") ?? "files.read, bot_*");
    const {
      instructions: _instructions,
      tools: _tools,
      ...rest
    } = bot?.definition ?? {};
    setAdvanced(
      JSON.stringify(
        {
          description: "",
          model_roles: {},
          allowed_nodes: [],
          delegates: null,
          resource_policy: {
            strategy: "pool",
            fixed_node_id: null,
            required_capabilities: [],
            workspace: "managed",
            max_concurrency: 1,
            offline_wait_seconds: 900,
          },
          output_contract: {
            require_text: true,
            min_files: 0,
            description: "",
          },
          ...rest,
        },
        null,
        2,
      ),
    );
  }
  function openConversation(conversationId: string) {
    if (onTask) onTask(conversationId);
    else sessionStorage.setItem("niucai.open-conversation", conversationId);
    setPage(onTask ? "computer" : "chat");
  }
  function stageLabel(w: Workflow, itemId: string) {
    const stage = w.limits?.stages?.find((st) => st.work_item_id === itemId);
    if (!stage) return null;
    return (
      <strong>
        [{stage.key}
        {stage.depends_on.length ? ` ← ${stage.depends_on.join("、")}` : ""}
        ]{" "}
      </strong>
    );
  }
  if (!connected) return <ConnectionEmpty />;
  return (
    <div className="settings-page">
      <Header
        eyebrow="COLLABORATION"
        title="Bots"
        description="角色、协作流程与定时工作"
      />
      <ErrorNotice
        error={
          status.error ||
          bots.error ||
          workflows.error ||
          items.error ||
          routines.error
        }
      />
      {demo && <p>请连接真实 Kernel 来管理 Bots。</p>}
      {status.data && (
        <p>
          协作功能：{status.data.enabled ? "已启用" : "未启用"} · 全部 Bot：
          {status.data.stopped ? "已停止" : "可执行"}
          {status.data.stopped &&
            (status.data.resource_release === "confirmed"
              ? " · 执行资源已释放"
              : status.data.resource_release === "unknown"
                ? ` · ${status.data.legacy_untracked_runs?.length ?? 0} 条历史运行未记录退出，资源释放无法确认`
                : ` · 等待 ${status.data.unconfirmed_local_runs.length} 个本地执行和 ${status.data.unconfirmed_remote_jobs.length} 个远程工作确认退出`)}
        </p>
      )}
      <section className="panel" style={{ padding: 20 }}>
        <h2>角色</h2>
        <button onClick={() => edit()}>新建 Bot</button>
        {bots.data
          ?.filter((b) => !b.definition.archived)
          .map((bot) => (
            <div key={bot.id} style={{ margin: "12px 0" }}>
              <button onClick={() => edit(bot)}>
                {bot.name} · {bot.id}
              </button>
              <span> {bot.definition.description}</span>{" "}
              <small>{binding(bot)}</small>
              {bot.id !== "main" && (
                <button
                  disabled={busy}
                  onClick={() =>
                    void act(() => request(`/api/agents/${bot.id}`, "DELETE"))
                  }
                >
                  归档
                </button>
              )}
            </div>
          ))}
        <form
          onSubmit={(event) => {
            event.preventDefault();
            void act(async () => {
              const old =
                bots.data?.find((b) => b.id === selected)?.definition ?? {};
              const definition = {
                ...old,
                ...JSON.parse(advanced),
                instructions,
                tools: tools
                  .split(",")
                  .map((s) => s.trim())
                  .filter(Boolean),
              };
              await request(
                selected ? `/api/agents/${selected}` : "/api/agents",
                selected ? "PATCH" : "POST",
                selected ? { name, definition } : { id, name, definition },
              );
              notify("Bot 已保存");
            });
          }}
        >
          <p>
            <label>
              ID{" "}
              <input
                required
                disabled={!!selected}
                value={id}
                onChange={(e) => setId(e.target.value)}
              />
            </label>
          </p>
          <p>
            <label>
              名称{" "}
              <input
                required
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
            </label>
          </p>
          <p>
            <label>
              角色规则{" "}
              <textarea
                value={instructions}
                onChange={(e) => setInstructions(e.target.value)}
                rows={5}
              />
            </label>
          </p>
          <p>
            <label>
              允许工具（逗号分隔）
              <textarea
                value={tools}
                onChange={(e) => setTools(e.target.value)}
                rows={2}
              />
            </label>
          </p>
          <p>
            <label>
              节点策略
              <select
                value={resourceConfig().resource_policy?.strategy ?? "pool"}
                onChange={(e) =>
                  updateResource(
                    e.target.value,
                    e.target.value === "fixed"
                      ? (nodes.data?.[0]?.id ?? null)
                      : null,
                  )
                }
              >
                <option value="pool">授权池</option>
                <option value="fixed">固定节点</option>
              </select>
            </label>
          </p>
          {resourceConfig().resource_policy?.strategy === "fixed" && (
            <p>
              <label>
                固定节点
                <select
                  value={resourceConfig().resource_policy?.fixed_node_id ?? ""}
                  onChange={(e) => updateResource("fixed", e.target.value)}
                >
                  <option value="">请选择节点</option>
                  {nodes.data?.map((node) => (
                    <option key={node.id} value={node.id}>
                      {node.name} · {node.connection_status}
                    </option>
                  ))}
                </select>
              </label>
            </p>
          )}
          <p>
            <label>
              可用技能（逗号分隔，支持 *；留空=全部，none=禁止）
              <input
                aria-label="可用技能"
                value={listText(resourceConfig().skills)}
                onChange={(e) =>
                  setDefinitionField("skills", parseList(e.target.value))
                }
              />
            </label>
          </p>
          <p>
            <label>
              可用 MCP 服务器（留空=全部，none=禁止）
              <input
                aria-label="可用 MCP 服务器"
                value={listText(resourceConfig().mcp_servers)}
                onChange={(e) =>
                  setDefinitionField("mcp_servers", parseList(e.target.value))
                }
              />
            </label>
          </p>
          <p>允许节点（新 Bot 需显式选择）</p>
          {nodes.data?.map((node) => (
            <label key={node.id}>
              <input
                type="checkbox"
                checked={
                  resourceConfig().allowed_nodes?.includes(node.id) ?? false
                }
                onChange={(e) => {
                  const old = JSON.parse(advanced);
                  const allowed = old.allowed_nodes ?? [];
                  setAdvanced(
                    JSON.stringify(
                      {
                        ...old,
                        allowed_nodes: e.target.checked
                          ? [...allowed, node.id]
                          : allowed.filter((id: string) => id !== node.id),
                      },
                      null,
                      2,
                    ),
                  );
                }}
              />
              {node.name} · {node.connection_status}
            </label>
          ))}
          <button disabled={busy || !enabled}>保存角色</button>
          <details>
            <summary>高级配置（模型、资源与交付）</summary>
            <label>
              <textarea
                aria-label="模型、资源与交付配置"
                rows={14}
                value={advanced}
                onChange={(e) => setAdvanced(e.target.value)}
              />
            </label>
          </details>
          <p>
            fixed 指定 fixed_node_id 且必须列入 allowed_nodes；pool 仅在
            allowed_nodes 内选择。空列表禁止远程节点，null
            保留兼容含义。离线节点保存后仍待验证。中央 Bot
            推理与节点模型分别运行。
          </p>
        </form>
        <p>
          <button
            disabled={busy || !enabled}
            onClick={() =>
              void act(() => request("/api/agents/stop-all", "POST"))
            }
          >
            停止全部 Bot
          </button>{" "}
          <button
            disabled={busy || !enabled}
            onClick={() =>
              void act(() => request("/api/agents/resume-all", "POST"))
            }
          >
            解除停止闸门
          </button>
        </p>
      </section>
      <section className="panel" style={{ padding: 20, marginTop: 16 }}>
        <h2>协作看板</h2>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            void act(async () => {
              await request("/api/workflows", "POST", {
                goal,
                participant_agent_ids: participants,
                output_requirements: delivery,
              });
              setGoal("");
            });
          }}
        >
          <label>
            主任务
            <textarea
              required
              value={goal}
              onChange={(e) => setGoal(e.target.value)}
            />
          </label>
          <label>
            最终交付要求
            <textarea
              value={delivery}
              onChange={(e) => setDelivery(e.target.value)}
            />
          </label>
          <p>参与 Bot</p>
          {bots.data
            ?.filter((b) => b.id !== "main" && !b.definition.archived)
            .map((b) => (
              <label key={b.id}>
                <input
                  type="checkbox"
                  checked={participants.includes(b.id)}
                  onChange={(e) =>
                    setParticipants(
                      e.target.checked
                        ? [...participants, b.id]
                        : participants.filter((id) => id !== b.id),
                    )
                  }
                />
                {b.name}
              </label>
            ))}
          <button disabled={busy || !enabled}>创建主任务</button>
        </form>
        {workflows.data?.map((w) => (
          <div key={w.id}>
            <p>
              {w.owner_agent_id} <Badge status={w.status} /> · 交接{" "}
              {w.handoff_count} · 模型轮次 {w.model_turns}
              {!!w.limits?.stages?.length && (
                <>
                  {" "}
                  · 依赖流程：
                  {w.limits.stages
                    .map((st) =>
                      st.depends_on.length
                        ? `${st.depends_on.join("+")} → ${st.key}`
                        : st.key,
                    )
                    .join("，")}
                </>
              )}
            </p>
            {["ACTIVE", "PAUSED"].includes(w.status) && (
              <p>
                {[w.status === "PAUSED" ? "resume" : "pause", "cancel"].map(
                  (op) => (
                    <button
                      key={op}
                      disabled={busy}
                      onClick={() =>
                        void act(() =>
                          request(`/api/workflows/${w.id}/${op}`, "POST"),
                        )
                      }
                    >
                      {op === "pause"
                        ? "暂停流程"
                        : op === "resume"
                          ? "恢复流程"
                          : "取消流程"}
                    </button>
                  ),
                )}
              </p>
            )}
            {!!w.limits?.stages?.length && (
              <WorkflowGraph
                stages={w.limits.stages}
                items={items.data?.filter((i) => i.workflow_id === w.id) ?? []}
                botName={(agentId) =>
                  bots.data?.find((b) => b.id === agentId)?.name ?? agentId
                }
                nodeLabel={(agentId, nodeId) => {
                  const id =
                    nodeId ??
                    bots.data?.find((b) => b.id === agentId)?.definition
                      .resource_policy?.fixed_node_id;
                  if (!id) return "Kernel 本机";
                  return `节点 ${nodes.data?.find((n) => n.id === id)?.name ?? id}`;
                }}
                onOpen={openConversation}
              />
            )}
            {items.data
              ?.filter((i) => i.workflow_id === w.id)
              .map((i) => (
                <p key={i.id}>
                  {stageLabel(w, i.id)}
                  {i.assignee_agent_id} <Badge status={i.status} /> {i.goal}{" "}
                  <small>
                    配置 v
                    {i.definition_snapshot?.definition_version ?? "未记录"}
                  </small>
                  {i.remote_jobs?.map((job) => (
                    <span key={job.id}>
                      {" "}
                      · 节点 {job.node_id ?? "尚未派发"} · {job.status} ·{" "}
                      {job.wait_state?.reason}
                    </span>
                  ))}
                  {i.artifacts?.map((a) => (
                    <span key={a.id}>
                      {" "}
                      · 产物 {a.id} / {a.content_hash.slice(0, 12)}
                    </span>
                  ))}
                  <button onClick={() => openConversation(i.conversation_id)}>
                    查看执行会话
                  </button>
                </p>
              ))}
          </div>
        ))}
      </section>
      <section className="panel" style={{ padding: 20, marginTop: 16 }}>
        <h2>依赖流程</h2>
        <p>
          把任务拆成多个阶段交给不同
          Bot：下游阶段在全部上游完成后启动，并自动收到上游结论与产物；任一上游失败，下游自动取消。
        </p>
        <form
          aria-label="依赖流程"
          onSubmit={(event) => {
            event.preventDefault();
            void act(async () => {
              await request("/api/workflows", "POST", {
                goal: pipelineGoal,
                stages: stages.map((stage) => ({
                  ...stage,
                  key: stage.key.trim(),
                })),
              });
              setPipelineGoal("");
              setStages([newStage(1)]);
              notify("依赖流程已创建");
            });
          }}
        >
          <label>
            总目标
            <textarea
              required
              aria-label="流程总目标"
              value={pipelineGoal}
              onChange={(e) => setPipelineGoal(e.target.value)}
            />
          </label>
          {stages.map((stage, index) => (
            <fieldset className="pipeline-stage" key={index}>
              <legend>阶段 {index + 1}</legend>
              <label>
                阶段名{" "}
                <input
                  required
                  aria-label={`阶段 ${index + 1} 名称`}
                  pattern="[a-zA-Z0-9][a-zA-Z0-9_-]{0,39}"
                  title="字母或数字开头，可含 _ 与 -"
                  value={stage.key}
                  onChange={(e) => changeStage(index, { key: e.target.value })}
                />
              </label>
              <label>
                负责 Bot{" "}
                <select
                  required
                  aria-label={`阶段 ${index + 1} Bot`}
                  value={stage.agent_id}
                  onChange={(e) =>
                    changeStage(index, { agent_id: e.target.value })
                  }
                >
                  <option value="">请选择</option>
                  {bots.data
                    ?.filter((b) => !b.definition.archived)
                    .map((b) => (
                      <option key={b.id} value={b.id}>
                        {b.name} · {binding(b)}
                      </option>
                    ))}
                </select>
              </label>
              <label>
                阶段目标
                <textarea
                  required
                  aria-label={`阶段 ${index + 1} 目标`}
                  value={stage.goal}
                  onChange={(e) => changeStage(index, { goal: e.target.value })}
                />
              </label>
              <label>
                交付要求
                <input
                  aria-label={`阶段 ${index + 1} 交付要求`}
                  value={stage.output_requirements}
                  onChange={(e) =>
                    changeStage(index, { output_requirements: e.target.value })
                  }
                />
              </label>
              {stages.length > 1 && (
                <div>
                  依赖：
                  {stages
                    .filter((_, i) => i !== index)
                    .map((other) => (
                      <label key={other.key}>
                        <input
                          type="checkbox"
                          checked={stage.depends_on.includes(other.key)}
                          onChange={(e) =>
                            changeStage(index, {
                              depends_on: e.target.checked
                                ? [...stage.depends_on, other.key]
                                : stage.depends_on.filter(
                                    (d) => d !== other.key,
                                  ),
                            })
                          }
                        />
                        {other.key}
                      </label>
                    ))}
                </div>
              )}
              {stages.length > 1 && (
                <button type="button" onClick={() => removeStage(index)}>
                  删除阶段
                </button>
              )}
            </fieldset>
          ))}
          <p>
            <button
              type="button"
              disabled={stages.length >= 12}
              onClick={() =>
                setStages((current) => [
                  ...current,
                  newStage(current.length + 1, current.at(-1)?.key),
                ])
              }
            >
              添加阶段
            </button>{" "}
            <button disabled={busy || !enabled}>创建依赖流程</button>
          </p>
        </form>
      </section>
      <section className="panel" style={{ padding: 20, marginTop: 16 }}>
        <h2>定时工作</h2>
        {routines.data?.map((r) => (
          <p key={r.id}>
            {r.agent_id} · {r.schedule} · {r.timezone} ·{" "}
            {r.enabled ? r.next_run_at : "已停用"}{" "}
            <button
              disabled={busy}
              onClick={() =>
                void act(() => request(`/api/routines/${r.id}`, "DELETE"))
              }
            >
              停用
            </button>
          </p>
        ))}
        <form
          onSubmit={(event) => {
            event.preventDefault();
            void act(() =>
              request("/api/routines", "POST", {
                agent_id: routineBot,
                schedule: cron,
                timezone,
                prompt,
              }),
            );
          }}
        >
          <p>
            <label>
              Bot{" "}
              <select
                value={routineBot}
                onChange={(e) => setRoutineBot(e.target.value)}
              >
                {bots.data
                  ?.filter((b) => !b.definition.archived)
                  .map((b) => (
                    <option key={b.id} value={b.id}>
                      {b.name}
                    </option>
                  ))}
              </select>
            </label>
          </p>
          <p>
            <label>
              Cron{" "}
              <input
                required
                value={cron}
                onChange={(e) => setCron(e.target.value)}
              />
            </label>{" "}
            <label>
              时区{" "}
              <input
                required
                value={timezone}
                onChange={(e) => setTimezone(e.target.value)}
              />
            </label>
          </p>
          <p>
            <label>
              工作要求{" "}
              <textarea
                required
                value={prompt}
                onChange={(e) => setPrompt(e.target.value)}
              />
            </label>
          </p>
          <button disabled={busy || !enabled}>创建定时工作</button>
        </form>
      </section>
    </div>
  );
}
