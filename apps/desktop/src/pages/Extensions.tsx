import { useState } from "react";
import { Header, ErrorNotice, ConnectionEmpty, Badge } from "../components/ui";
import { request } from "../lib/api";
import { useApp, useCommand, useData } from "../lib/state";

export interface Skill {
  id: string;
  name: string;
  description: string;
  content: string;
  version: number;
  enabled: boolean;
}
interface McpTool {
  name: string;
  description: string;
  annotations?: { readOnlyHint?: boolean };
}
export interface McpServer {
  id: string;
  name: string;
  transport: "stdio" | "http";
  config: {
    command?: string;
    args?: string[];
    cwd?: string | null;
    env?: Record<string, string>;
    url?: string;
    headers?: Record<string, string>;
  };
  tools: McpTool[];
  enabled: boolean;
  auto_approve_read_only: boolean;
  status: {
    ok?: boolean | null;
    tool_count?: number;
    error?: string;
    note?: string;
    refreshed_at?: string;
  };
}

/** “KEY=VALUE” / “Key: value” 逐行文本与对象互转；密钥值由 Kernel 以 *** 回显并在保存时保留。 */
export function parsePairs(text: string, separator: "=" | ":") {
  const pairs: Record<string, string> = {};
  for (const line of text.split("\n")) {
    const at = line.indexOf(separator);
    if (at <= 0) continue;
    pairs[line.slice(0, at).trim()] = line.slice(at + 1).trim();
  }
  return pairs;
}
export function formatPairs(
  pairs: Record<string, string> | undefined,
  separator: "=" | ":",
) {
  return Object.entries(pairs ?? {})
    .map(([k, v]) => `${k}${separator === ":" ? ": " : "="}${v}`)
    .join("\n");
}

const EMPTY_SKILL = { name: "", description: "", content: "", enabled: true };
const EMPTY_SERVER = {
  name: "",
  transport: "stdio" as McpServer["transport"],
  command: "",
  args: "",
  cwd: "",
  env: "",
  url: "",
  headers: "",
  enabled: true,
  auto_approve_read_only: true,
};

function serverStatus(server: McpServer) {
  const s = server.status ?? {};
  if (s.ok === true)
    return `可用 · ${s.tool_count ?? server.tools.length} 个工具`;
  if (s.ok === false) return `刷新失败：${s.error ?? "未知错误"}`;
  if (s.note) return "配置已修改，需要重新刷新工具";
  return "尚未刷新工具";
}

export function Extensions() {
  const { connected, demo } = useApp();
  const skills = useData(["skills"], () => request<Skill[]>("/api/skills"));
  const servers = useData(["mcp-servers"], () =>
    request<{ enabled: boolean; servers: McpServer[] }>("/api/mcp/servers"),
  );
  const [skillId, setSkillId] = useState<string | null>(null);
  const [skill, setSkill] = useState(EMPTY_SKILL);
  const [serverId, setServerId] = useState<string | null>(null);
  const [server, setServer] = useState(EMPTY_SERVER);

  const saveSkill = useCommand(async () => {
    await request(
      skillId ? `/api/skills/${skillId}` : "/api/skills",
      skillId ? "PATCH" : "POST",
      skill,
    );
    setSkillId(null);
    setSkill(EMPTY_SKILL);
  }, "技能已保存");
  const skillCommand = useCommand(
    ({ id, body }: { id: string; body?: Partial<Skill> }) =>
      body
        ? request(`/api/skills/${id}`, "PATCH", body)
        : request(`/api/skills/${id}`, "DELETE"),
    "技能已更新",
  );
  const saveServer = useCommand(async () => {
    const config =
      server.transport === "stdio"
        ? {
            command: server.command.trim(),
            args: server.args
              .split("\n")
              .map((a) => a.trim())
              .filter(Boolean),
            env: parsePairs(server.env, "="),
            cwd: server.cwd.trim() || null,
          }
        : { url: server.url.trim(), headers: parsePairs(server.headers, ":") };
    const body = {
      name: server.name,
      config,
      enabled: server.enabled,
      auto_approve_read_only: server.auto_approve_read_only,
    };
    await request(
      serverId ? `/api/mcp/servers/${serverId}` : "/api/mcp/servers",
      serverId ? "PATCH" : "POST",
      serverId ? body : { ...body, transport: server.transport },
    );
    setServerId(null);
    setServer(EMPTY_SERVER);
  }, "MCP 服务器已保存");
  const serverCommand = useCommand(
    ({ id, op, body }: { id: string; op: string; body?: object }) =>
      op === "refresh"
        ? request(`/api/mcp/servers/${id}/refresh`, "POST")
        : op === "delete"
          ? request(`/api/mcp/servers/${id}`, "DELETE")
          : request(`/api/mcp/servers/${id}`, "PATCH", body),
    "MCP 服务器已更新",
  );
  const busy =
    saveSkill.isPending ||
    skillCommand.isPending ||
    saveServer.isPending ||
    serverCommand.isPending;

  function editSkill(row?: Skill) {
    setSkillId(row?.id ?? null);
    setSkill(
      row
        ? {
            name: row.name,
            description: row.description,
            content: row.content,
            enabled: row.enabled,
          }
        : EMPTY_SKILL,
    );
  }
  function editServer(row?: McpServer) {
    setServerId(row?.id ?? null);
    setServer(
      row
        ? {
            name: row.name,
            transport: row.transport,
            command: row.config.command ?? "",
            args: (row.config.args ?? []).join("\n"),
            cwd: row.config.cwd ?? "",
            env: formatPairs(row.config.env, "="),
            url: row.config.url ?? "",
            headers: formatPairs(row.config.headers, ":"),
            enabled: row.enabled,
            auto_approve_read_only: row.auto_approve_read_only,
          }
        : EMPTY_SERVER,
    );
  }

  if (!connected) return <ConnectionEmpty />;
  return (
    <div className="settings-page">
      <Header
        eyebrow="EXTENSIONS"
        title="技能与 MCP"
        description="所有 Bot 共享的技能库与 MCP 工具；Bot 设置中的白名单可再收窄"
      />
      <ErrorNotice error={skills.error || servers.error} />

      <section className="panel" style={{ padding: 20 }} aria-label="技能库">
        <h2>技能库</h2>
        <p>
          技能是给 Bot 的操作说明（如抓取规范、报告模板）。Bot
          在上下文中看到技能目录，需要时用 skills.read
          读取全文，只读、无需审批。
        </p>
        {skills.data?.length === 0 && <p>还没有技能。</p>}
        {skills.data?.map((row) => (
          <div key={row.id} style={{ margin: "10px 0" }}>
            <strong>{row.name}</strong> <small>v{row.version}</small>{" "}
            <Badge status={row.enabled ? "ONLINE" : "OFFLINE"} />{" "}
            <span>{row.description}</span>{" "}
            <button disabled={busy || demo} onClick={() => editSkill(row)}>
              编辑
            </button>{" "}
            <button
              disabled={busy || demo}
              onClick={() =>
                skillCommand.mutate({
                  id: row.id,
                  body: { enabled: !row.enabled },
                })
              }
            >
              {row.enabled ? "停用" : "启用"}
            </button>{" "}
            <button
              disabled={busy || demo}
              onClick={() => {
                if (window.confirm(`删除技能 ${row.name}？此操作不可撤销。`))
                  skillCommand.mutate({ id: row.id });
              }}
            >
              删除
            </button>
          </div>
        ))}
        <form
          aria-label="技能编辑"
          onSubmit={(e) => {
            e.preventDefault();
            saveSkill.mutate(undefined);
          }}
        >
          <h3>{skillId ? `编辑技能 ${skill.name}` : "新建技能"}</h3>
          <p>
            <label>
              名称{" "}
              <input
                required
                aria-label="技能名称"
                pattern="[a-zA-Z0-9][a-zA-Z0-9_.\-]{0,99}"
                title="字母或数字开头，可含 _ . -"
                value={skill.name}
                onChange={(e) => setSkill({ ...skill, name: e.target.value })}
              />
            </label>
          </p>
          <p>
            <label>
              简介{" "}
              <input
                aria-label="技能简介"
                value={skill.description}
                maxLength={1000}
                onChange={(e) =>
                  setSkill({ ...skill, description: e.target.value })
                }
              />
            </label>
          </p>
          <p>
            <label>
              内容
              <textarea
                aria-label="技能内容"
                rows={8}
                value={skill.content}
                onChange={(e) =>
                  setSkill({ ...skill, content: e.target.value })
                }
              />
            </label>
          </p>
          <button className="primary" disabled={busy || demo}>
            保存技能
          </button>{" "}
          {skillId && (
            <button type="button" onClick={() => editSkill()}>
              取消编辑
            </button>
          )}
        </form>
      </section>

      <section
        className="panel"
        style={{ padding: 20, marginTop: 16 }}
        aria-label="MCP 服务器"
      >
        <h2>MCP 服务器</h2>
        <p>
          {servers.data?.enabled
            ? "Kernel 已启用 MCP。"
            : "Kernel 未启用 MCP（NIUCAI_MCP_ENABLED），可以先登记服务器，启用后才能刷新与调用。"}
          标记为只读的工具在开启“只读免审批”时直接执行，其余调用需要人工审批（或会话
          YOLO）。stdio 服务器以 Kernel
          进程身份运行，不是安全沙箱，只登记可信命令。
        </p>
        {servers.data?.servers.length === 0 && <p>还没有 MCP 服务器。</p>}
        {servers.data?.servers.map((row) => (
          <div key={row.id} className="pipeline-stage">
            <div>
              <strong>{row.name}</strong>{" "}
              <small>
                {row.transport === "stdio"
                  ? `本地命令 ${row.config.command ?? ""}`
                  : `HTTP ${row.config.url ?? ""}`}
              </small>{" "}
              <Badge status={row.enabled ? "ONLINE" : "OFFLINE"} />
            </div>
            <small>
              {serverStatus(row)}
              {row.auto_approve_read_only ? " · 只读工具免审批" : ""}
            </small>
            <div>
              <button
                disabled={busy || demo || !servers.data?.enabled}
                onClick={() =>
                  serverCommand.mutate({ id: row.id, op: "refresh" })
                }
              >
                刷新工具
              </button>{" "}
              <button disabled={busy || demo} onClick={() => editServer(row)}>
                编辑
              </button>{" "}
              <button
                disabled={busy || demo}
                onClick={() =>
                  serverCommand.mutate({
                    id: row.id,
                    op: "patch",
                    body: { enabled: !row.enabled },
                  })
                }
              >
                {row.enabled ? "停用" : "启用"}
              </button>{" "}
              <button
                disabled={busy || demo}
                onClick={() => {
                  if (window.confirm(`删除 MCP 服务器 ${row.name}？`))
                    serverCommand.mutate({ id: row.id, op: "delete" });
                }}
              >
                删除
              </button>
            </div>
            {row.tools.length > 0 && (
              <details>
                <summary>{row.tools.length} 个工具</summary>
                {row.tools.map((tool) => (
                  <p key={tool.name}>
                    <strong>{tool.name}</strong>
                    {tool.annotations?.readOnlyHint ? " · 只读" : " · 需审批"}
                    {tool.description ? ` · ${tool.description}` : ""}
                  </p>
                ))}
              </details>
            )}
          </div>
        ))}
        <form
          aria-label="MCP 服务器编辑"
          onSubmit={(e) => {
            e.preventDefault();
            saveServer.mutate(undefined);
          }}
        >
          <h3>{serverId ? `编辑 ${server.name}` : "登记 MCP 服务器"}</h3>
          <p>
            <label>
              名称{" "}
              <input
                required
                aria-label="服务器名称"
                pattern="[a-zA-Z0-9][a-zA-Z0-9_.\-]{0,99}"
                value={server.name}
                onChange={(e) => setServer({ ...server, name: e.target.value })}
              />
            </label>{" "}
            <label>
              连接方式{" "}
              <select
                aria-label="连接方式"
                disabled={!!serverId}
                value={server.transport}
                onChange={(e) =>
                  setServer({
                    ...server,
                    transport: e.target.value as McpServer["transport"],
                  })
                }
              >
                <option value="stdio">本地命令（stdio）</option>
                <option value="http">远程地址（Streamable HTTP）</option>
              </select>
            </label>
          </p>
          {server.transport === "stdio" ? (
            <>
              <p>
                <label>
                  命令{" "}
                  <input
                    required
                    aria-label="启动命令"
                    placeholder="npx"
                    value={server.command}
                    onChange={(e) =>
                      setServer({ ...server, command: e.target.value })
                    }
                  />
                </label>{" "}
                <label>
                  工作目录{" "}
                  <input
                    aria-label="工作目录"
                    value={server.cwd}
                    onChange={(e) =>
                      setServer({ ...server, cwd: e.target.value })
                    }
                  />
                </label>
              </p>
              <p>
                <label>
                  参数（每行一个）
                  <textarea
                    aria-label="启动参数"
                    rows={3}
                    value={server.args}
                    onChange={(e) =>
                      setServer({ ...server, args: e.target.value })
                    }
                  />
                </label>
              </p>
              <p>
                <label>
                  环境变量（每行 KEY=VALUE，已保存的值显示为 ***）
                  <textarea
                    aria-label="环境变量"
                    rows={3}
                    value={server.env}
                    onChange={(e) =>
                      setServer({ ...server, env: e.target.value })
                    }
                  />
                </label>
              </p>
            </>
          ) : (
            <>
              <p>
                <label>
                  地址{" "}
                  <input
                    required
                    type="url"
                    aria-label="服务器地址"
                    placeholder="https://example.com/mcp"
                    value={server.url}
                    onChange={(e) =>
                      setServer({ ...server, url: e.target.value })
                    }
                  />
                </label>
              </p>
              <p>
                <label>
                  请求头（每行 Key: value，已保存的值显示为 ***）
                  <textarea
                    aria-label="请求头"
                    rows={3}
                    value={server.headers}
                    onChange={(e) =>
                      setServer({ ...server, headers: e.target.value })
                    }
                  />
                </label>
              </p>
            </>
          )}
          <p>
            <label>
              <input
                type="checkbox"
                checked={server.enabled}
                onChange={(e) =>
                  setServer({ ...server, enabled: e.target.checked })
                }
              />
              启用
            </label>{" "}
            <label>
              <input
                type="checkbox"
                checked={server.auto_approve_read_only}
                onChange={(e) =>
                  setServer({
                    ...server,
                    auto_approve_read_only: e.target.checked,
                  })
                }
              />
              只读工具免审批
            </label>
          </p>
          <button className="primary" disabled={busy || demo}>
            保存服务器
          </button>{" "}
          {serverId && (
            <button type="button" onClick={() => editServer()}>
              取消编辑
            </button>
          )}
        </form>
      </section>
    </div>
  );
}
