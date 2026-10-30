import { useEffect, useState } from "react";
import { Check, Link2, LockKeyhole, Unplug } from "lucide-react";
import { native } from "../lib/api";
import { useApp } from "../lib/state";
import { Header } from "../components/ui";
import { KernelManager } from "../components/KernelManager";
import { ConnectionList } from "../components/Connections";
import { Bots } from "./Bots";
import { Extensions } from "./Extensions";
import { MemoryPage } from "./FilesMemory";
import type { Page } from "../lib/types";

const tabs = [
  ["kernel", "连接"],
  ["bots", "助手"],
  ["tools", "技能与工具"],
  ["memory", "偏好与记忆"],
  ["advanced", "高级"],
] as const;

/** 旧页面迁移入口：能力完整保留，只是不再占用主导航。 */
const legacyPages: { page: Page; title: string; hint: string }[] = [
  {
    page: "goals",
    title: "Goal 列表",
    hint: "按 Goal 查看编排记录与白板卡片。",
  },
  { page: "board", title: "白板", hint: "查看和手工创建白板卡片。" },
  { page: "kanban", title: "看板", hint: "旧版任务看板。" },
  {
    page: "remote",
    title: "远程 Pi",
    hint: "提交远程 Pi 任务、查看队列与结果。",
  },
];
export function Settings({
  onTask,
}: { onTask?: (id: string | null) => void } = {}) {
  const {
    setPage,
    profile,
    connected,
    demo,
    connectTo,
    disconnectFrom,
    notify,
    explore,
  } = useApp();
  const [name, setName] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [computerUrl, setComputerUrl] = useState("");
  const [token, setToken] = useState("");
  const [remember, setRemember] = useState(true);
  const [busy, setBusy] = useState(false);
  const [tab, setTab] = useState<(typeof tabs)[number][0]>("kernel");
  useEffect(() => {
    if (profile) {
      setName(profile.name ?? "");
      setBaseUrl(profile.base_url);
      setComputerUrl(profile.computer_url);
      setRemember(profile.remember_token);
    }
  }, [profile]);
  async function submit() {
    setBusy(true);
    try {
      await connectTo({
        baseUrl,
        computerUrl,
        token,
        rememberToken: remember,
        name: name.trim() || undefined,
      });
      setToken("");
    } catch (e) {
      notify(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }
  function addConnection() {
    setName("");
    setBaseUrl("");
    setComputerUrl("");
    setToken("");
    setRemember(true);
  }
  return (
    <>
      <Header
        eyebrow="SETTINGS"
        title="设置"
        description="Kernel 负责状态与授权；节点与 Bot 负责执行。"
      />
      <div className="settings-tabs" role="tablist">
        {tabs.map(([key, label]) => (
          <button
            key={key}
            role="tab"
            aria-selected={tab === key}
            className={tab === key ? "active" : ""}
            onClick={() => setTab(key)}
          >
            {label}
          </button>
        ))}
      </div>
      {tab === "bots" && <Bots onTask={onTask} />}
      {tab === "tools" && <Extensions />}
      {tab === "memory" && <MemoryPage />}
      {tab === "advanced" && (
        <section className="panel" aria-label="高级入口">
          <div className="section-heading">
            <h2>高级与迁移入口</h2>
          </div>
          <p className="muted">
            这些页面仍可使用，但日常任务不需要进入。执行环境与节点配对在“设备”页。
          </p>
          <div className="advanced-links">
            {legacyPages.map((item) => (
              <button key={item.page} onClick={() => setPage(item.page)}>
                <strong>{item.title}</strong>
                <small>{item.hint}</small>
              </button>
            ))}
          </div>
        </section>
      )}
      <div className="settings-grid" hidden={tab !== "kernel"}>
        <KernelManager />
        <ConnectionList onAdd={addConnection} />
        <section className="panel">
          <div className="section-heading">
            <h2>
              <Link2 size={18} />
              服务器连接
            </h2>
            {connected && (
              <span className="badge completed">
                <Check size={12} />
                {demo ? "示例模式" : "已连接"}
              </span>
            )}
          </div>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void submit();
            }}
          >
            <label>
              连接名称
              <input
                value={name}
                maxLength={40}
                onChange={(e) => setName(e.target.value)}
                placeholder="例如：本机、公司服务器"
              />
            </label>
            <label>
              Kernel 地址
              <input
                type="url"
                required
                value={baseUrl}
                onChange={(e) => setBaseUrl(e.target.value)}
                placeholder="https://agent.example.com"
                autoCapitalize="none"
                spellCheck={false}
              />
              <small>
                填写 HTTPS 地址；本机调试可使用 http://127.0.0.1:8080。
              </small>
            </label>
            <label>
              API Token
              <input
                type="password"
                value={token}
                onChange={(e) => setToken(e.target.value)}
                placeholder={
                  profile?.has_token
                    ? "已保存，留空以继续使用"
                    : "填入 Kernel 的连接凭据"
                }
                autoComplete="off"
              />
              <small>Token 只用于连接 Kernel，不是 OpenRouter API Key。</small>
            </label>
            <label>
              远程桌面地址（可稍后填写）
              <input
                type="url"
                value={computerUrl}
                onChange={(e) => setComputerUrl(e.target.value)}
                placeholder="https://agent.example.com/computer"
                autoCapitalize="none"
                spellCheck={false}
              />
              <small>填写 KasmVNC 网页地址。其登录由远程桌面自己处理。</small>
            </label>
            <label className="checkbox">
              <input
                type="checkbox"
                checked={remember}
                onChange={(e) => setRemember(e.target.checked)}
              />
              将 Token 保存到 Windows 凭据管理器
            </label>
            <div className="button-row">
              <button className="primary" disabled={busy || !native()}>
                {busy ? "正在验证连接…" : "验证并连接"}
              </button>
              {connected && (
                <button
                  type="button"
                  disabled={busy}
                  onClick={() =>
                    void disconnectFrom().catch((e) => notify(String(e)))
                  }
                >
                  <Unplug size={15} />
                  断开并清除凭据
                </button>
              )}
            </div>
            {!native() && (
              <p className="muted">
                网页预览仅支持示例；连接真实服务器请使用 Windows 安装包。
              </p>
            )}
          </form>
        </section>
        <div>
          <section className="panel">
            <div className="section-heading">
              <h2>
                <LockKeyhole size={18} />
                连接与控制
              </h2>
            </div>
            <ul className="settings-notes">
              <li>API Token 在原生层使用，服务器地址单独保存。</li>
              <li>连接公网 Kernel 使用 HTTPS，不将 Token 放入 URL。</li>
              <li>接管成功后才会打开远程桌面；关闭窗口不会自动交还 Agent。</li>
              <li>退出客户端不会取消已提交到 Kernel 的任务。</li>
            </ul>
          </section>
          <section className="panel">
            <h2>还没配置服务器？</h2>
            <p className="muted">
              先体验任务、审批与记忆界面。示例不会访问模型或设备。
            </p>
            <button onClick={explore}>查看示例工作台</button>
          </section>
          <section className="panel version">
            <span>niucai Desktop</span>
            <strong>v0.1.0</strong>
            <small>Windows x64 · Tauri 2 · WebView2</small>
          </section>
        </div>
      </div>
    </>
  );
}
