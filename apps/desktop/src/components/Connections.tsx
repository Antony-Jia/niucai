import { useEffect, useState } from "react";
import { Plus, RefreshCw, Trash2 } from "lucide-react";
import { useApp } from "../lib/state";
import { connectionKey, type ProbeStatus } from "../lib/types";

const STATUS: Record<ProbeStatus, string> = {
  online: "在线",
  offline: "无法访问",
  auth: "Token 失效",
  no_token: "未保存 Token",
};

export function ConnectionList({ onAdd }: { onAdd: () => void }) {
  const {
    connections,
    profile,
    connected,
    demo,
    probes,
    probe,
    switchTo,
    removeSaved,
    notify,
  } = useApp();
  const [busy, setBusy] = useState("");
  useEffect(() => {
    if (connections.length) void probe();
  }, [connections.length, probe]);
  const activeKey = profile && connected && !demo ? connectionKey(profile) : "";
  async function run(key: string, action: () => Promise<void>) {
    setBusy(key);
    try {
      await action();
    } catch (error) {
      notify(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy("");
    }
  }
  return (
    <section className="panel" style={{ padding: 20 }}>
      <div className="section-heading">
        <h2>已保存的连接</h2>
        <div className="button-row">
          <button
            type="button"
            onClick={() => void probe()}
            aria-label="检测连接状态"
          >
            <RefreshCw size={14} />
            检测
          </button>
          <button type="button" onClick={onAdd}>
            <Plus size={14} />
            添加连接
          </button>
        </div>
      </div>
      {connections.length === 0 ? (
        <p className="muted">
          还没有保存的连接。可同时保存本机和服务器，在两者之间随时切换。
        </p>
      ) : (
        <ul className="connection-list">
          {connections.map((item) => {
            const key = connectionKey(item);
            const state = probes[key]?.status;
            const active = key === activeKey;
            return (
              <li key={key} data-active={active}>
                <div>
                  <strong>{item.name || item.base_url}</strong>
                  <small>{item.base_url}</small>
                </div>
                <span
                  className={`badge ${state === "online" ? "completed" : ""}`}
                >
                  {active ? "当前 · " : ""}
                  {state ? STATUS[state] : "检测中"}
                </span>
                <div className="button-row">
                  <button
                    type="button"
                    disabled={active || busy === key}
                    onClick={() => void run(key, () => switchTo(key))}
                  >
                    {active ? "使用中" : "切换"}
                  </button>
                  <button
                    type="button"
                    aria-label={`删除连接 ${item.name || item.base_url}`}
                    disabled={busy === key}
                    onClick={() => void run(key, () => removeSaved(key))}
                  >
                    <Trash2 size={14} />
                  </button>
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

export function ConnectionSwitcher() {
  const { connections, profile, connected, demo, switchTo, notify } = useApp();
  if (demo || connections.length < 2) return null;
  const current = profile && connected ? connectionKey(profile) : "";
  return (
    <label className="connection-switch">
      <span>切换 Kernel</span>
      <select
        value={current}
        aria-label="切换 Kernel"
        onChange={(e) =>
          void switchTo(e.target.value).catch((error) =>
            notify(error instanceof Error ? error.message : String(error)),
          )
        }
      >
        {!current && <option value="">选择连接…</option>}
        {connections.map((item) => (
          <option key={connectionKey(item)} value={connectionKey(item)}>
            {item.name || item.base_url}
          </option>
        ))}
      </select>
    </label>
  );
}
