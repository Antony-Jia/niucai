import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useApp } from "../lib/state";
import {
  boardApi,
  splitList,
  type Bot,
  type BotConfig,
  type Engine,
  type PermissionProfile,
} from "../lib/board";
import { Modal } from "./ui";

const engineText: Record<Engine, string> = {
  pi: "Pi",
  codex: "Codex",
  claude: "Claude Code",
};
const profileText: Record<PermissionProfile, string> = {
  read: "只读",
  workspace: "工作区内读写",
  full: "完全权限",
};

/** 新建或编辑节点上的 Bot；引擎只列出该节点上报可用的。 */
export function BotForm({
  nodeId,
  engines,
  bot,
  platform = "",
  onClose,
  onBack,
  nodeName,
  unavailable = false,
}: {
  nodeId: string;
  nodeName?: string;
  unavailable?: boolean;
  onBack?: () => void;
  engines: string[];
  bot?: Bot;
  platform?: string;
  onClose: () => void;
}) {
  // Windows 节点以用户身份运行，没有容器隔离：新 Bot 默认只读并需人工审批。
  const windows = platform === "windows";
  const { notify, synchronizing } = useApp();
  const cache = useQueryClient();
  const available = (["pi", "codex", "claude"] as Engine[]).filter(
    (e) => engines.includes(e) || bot?.engine === e,
  );
  const [form, setForm] = useState({
    name: bot?.name ?? "",
    engine: bot?.engine ?? available[0] ?? "pi",
    role: bot?.role ?? "",
    instructions: bot?.instructions ?? "",
    tags: bot?.tags.join(", ") ?? "",
    model: bot?.model ?? "",
    permission_profile:
      bot?.permission_profile ?? (windows ? "read" : "workspace"),
    skills: bot?.skills.join(", ") ?? "",
    mcp_servers: bot?.mcp_servers.join(", ") ?? "",
    requires_approval: bot?.requires_approval ?? windows,
    concurrency: bot?.concurrency ?? 1,
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const blocked = unavailable || synchronizing || available.length === 0;
  const set = <K extends keyof typeof form>(key: K, value: (typeof form)[K]) =>
    setForm((f) => ({ ...f, [key]: value }));
  async function save() {
    if (busy || blocked) return;
    setError("");
    setBusy(true);
    const config: BotConfig = {
      ...form,
      name: form.name.trim(),
      node_id: nodeId,
      engine: form.engine as Engine,
      permission_profile: form.permission_profile as PermissionProfile,
      tags: splitList(form.tags),
      skills: splitList(form.skills),
      mcp_servers: splitList(form.mcp_servers),
      concurrency: Math.min(16, Math.max(1, form.concurrency)),
    };
    try {
      if (bot) await boardApi.updateBot(bot.id, config);
      else await boardApi.createBot(config);
      await Promise.all([
        cache.invalidateQueries({ queryKey: ["topology"] }),
        cache.invalidateQueries({ queryKey: ["bots"] }),
        cache.invalidateQueries({ queryKey: ["board"] }),
      ]);
      notify(bot ? "Bot 已更新" : "Bot 已创建，可以到白板分配卡片");
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal title={bot ? `编辑 @${bot.name}` : "添加 Bot"} onClose={onClose}>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void save();
        }}
      >
        <div className="bot-form-context">
          <small className="eyebrow">
            {bot ? "BOT SETTINGS" : "STEP 02 / 02"}
          </small>
          <p>
            执行节点：<strong>{nodeName || nodeId}</strong>
          </p>
          {onBack && (
            <button type="button" onClick={onBack}>
              更换节点
            </button>
          )}
        </div>
        {blocked && (
          <p className="connection-help" role="status">
            连接或节点暂不可用。已填写内容保留在当前窗口，恢复后可继续保存。
          </p>
        )}
        {!bot && (
          <div className="bot-templates" role="group" aria-label="角色模板">
            {[
              {
                name: "通用助手",
                role: "通用执行助手",
                tags: "general",
                instructions:
                  "先确认目标与验收标准，按步骤完成卡片；遇到阻碍及时提问，提交结果与依据。",
              },
              {
                name: "资料研究",
                role: "研究与资料整理",
                tags: "research, docs",
                instructions:
                  "围绕卡片目标收集资料，标注来源与不确定性，提交结构清楚的结论。",
              },
              {
                name: "开发助手",
                role: "开发与验证",
                tags: "coding",
                instructions:
                  "阅读现有实现后完成卡片要求，保持改动聚焦，验证行为并报告结果。",
              },
            ].map((t) => (
              <button
                key={t.name}
                type="button"
                onClick={() =>
                  setForm((f) => ({
                    ...f,
                    role: t.role,
                    instructions: t.instructions,
                    tags: t.tags,
                  }))
                }
              >
                {t.name}
              </button>
            ))}
          </div>
        )}
        <label>
          名称（用于 @ 提及）
          <input
            required
            maxLength={60}
            pattern="[A-Za-z0-9_\-一-鿿]+"
            value={form.name}
            onChange={(e) => set("name", e.target.value)}
            placeholder="例如：coder"
          />
        </label>
        <label>
          引擎
          <select
            value={form.engine}
            onChange={(e) => set("engine", e.target.value as Engine)}
          >
            {available.map((e) => (
              <option key={e} value={e}>
                {engineText[e]}
              </option>
            ))}
          </select>
          {available.length === 0 && (
            <small>节点尚未上报可用引擎，保存后待节点上线再领取卡片。</small>
          )}
        </label>
        <label>
          角色
          <input
            maxLength={500}
            value={form.role}
            onChange={(e) => set("role", e.target.value)}
            placeholder="例如：前端开发"
          />
        </label>
        <label>
          指令
          <textarea
            rows={4}
            value={form.instructions}
            onChange={(e) => set("instructions", e.target.value)}
          />
        </label>
        <label>
          标签（领取公共池中带这些标签的卡片）
          <input
            value={form.tags}
            onChange={(e) => set("tags", e.target.value)}
            placeholder="frontend, docs"
          />
        </label>
        <label>
          权限
          <select
            value={form.permission_profile}
            onChange={(e) =>
              set("permission_profile", e.target.value as PermissionProfile)
            }
          >
            {(Object.keys(profileText) as PermissionProfile[]).map((p) => (
              <option key={p} value={p}>
                {profileText[p]}
              </option>
            ))}
          </select>
        </label>
        <details className="bot-advanced">
          <summary>高级配置 · 模型、技能与并发</summary>
          <label>
            模型（留空用节点默认）
            <input
              maxLength={200}
              value={form.model}
              onChange={(e) => set("model", e.target.value)}
            />
          </label>
          <label>
            技能
            <input
              value={form.skills}
              onChange={(e) => set("skills", e.target.value)}
              placeholder="技能名，逗号分隔"
            />
          </label>
          <label>
            MCP 服务器
            <input
              value={form.mcp_servers}
              onChange={(e) => set("mcp_servers", e.target.value)}
              placeholder="MCP 服务器名，逗号分隔"
            />
            {form.engine === "pi" && splitList(form.mcp_servers).length > 0 && (
              <small className="error-text">Pi Bot 暂不支持 MCP</small>
            )}
            {!!bot?.node_env?.length && (
              <small>需在节点上设置环境变量：{bot.node_env.join(", ")}</small>
            )}
            {!!bot?.missing_assets?.length && (
              <small className="error-text">
                未找到或已停用：{bot.missing_assets.join(", ")}
              </small>
            )}
          </label>
          <label>
            并发
            <input
              type="number"
              min={1}
              max={16}
              value={form.concurrency}
              onChange={(e) => set("concurrency", Number(e.target.value))}
            />
          </label>
        </details>
        <label className="checkbox">
          <input
            type="checkbox"
            checked={form.requires_approval}
            onChange={(e) => set("requires_approval", e.target.checked)}
          />
          每张卡片执行前需要我批准
        </label>
        {error && (
          <p className="error-notice" role="alert">
            {error}
          </p>
        )}
        <div className="button-row bot-form-actions">
          <button type="button" disabled={busy} onClick={onClose}>
            取消
          </button>
          <button
            className="primary"
            disabled={
              busy ||
              blocked ||
              !form.name.trim() ||
              (form.engine === "pi" && splitList(form.mcp_servers).length > 0)
            }
          >
            {busy ? "正在保存…" : "保存"}
          </button>
        </div>
      </form>
    </Modal>
  );
}
