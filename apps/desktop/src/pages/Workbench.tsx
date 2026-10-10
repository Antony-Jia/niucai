import { useEffect, useState } from "react";
import {
  Monitor,
  MousePointer2,
  CornerUpLeft,
  Pause,
  Play,
  Maximize2,
  Globe,
  Folder,
  Settings2,
} from "lucide-react";
import { api, native, changeComputerControl } from "../lib/api";
import { EmbeddedDesktop } from "../components/EmbeddedDesktop";
import { ComputerPreview } from "../components/ComputerPreview";
import { useApp, useCommand, useData } from "../lib/state";
import { Badge, ConnectionEmpty, ErrorNotice } from "../components/ui";
import { Chat } from "./Chat";
import { Files } from "./FilesMemory";
import { ComputerPage } from "./Computer";

export function Workbench({
  taskId,
  onTask,
}: {
  taskId: string | null;
  onTask: (id: string) => void;
}) {
  const { connected, demo, profile, setPage, notify, synchronizing } = useApp();
  const tasks = useData(["tasks"], api.tasks);
  const computers = useData(["computers"], api.computers);
  const controlStale = computers.isError || (!demo && synchronizing);
  const conversations = useData(["conversations"], api.conversations);
  const conversationId =
    tasks.data?.find((t) => t.id === taskId)?.conversation_id || taskId;
  const session = conversations.data?.find((c) => c.id === conversationId);
  const task = tasks.data?.find((t) => t.conversation_id === conversationId);
  const [selected, select] = useState("");
  const computer =
    computers.data?.find(
      (c) => c.id === (selected || session?.computer_id || task?.computer_id),
    ) || computers.data?.[0];
  const [tab, setTab] = useState("computer");
  const [view, setView] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const control = useCommand(async (operation: string) => {
    setView(false);
    if (!computer) throw new Error("请选择电脑");
    return changeComputerControl(computer.id, operation, notify);
  }, "控制权已更新");
  const open = useCommand(async () => {
    if (!computer) throw new Error("请选择电脑");
    const current = await api.computers();
    if (current.find((c) => c.id === computer.id)?.control !== "HUMAN")
      throw new Error("请先接管电脑");
    if (!demo && (!native() || !profile?.computer_url))
      throw new Error("请在 Windows 客户端设置远程桌面地址");
    notify("");
    setView(true);
  });
  useEffect(() => {
    setView(false);
  }, [computer?.id, computer?.control, connected, demo]);
  if (!connected)
    return (
      <>
        <h1>Agent 工作台</h1>
        <ConnectionEmpty />
      </>
    );
  return (
    <div className={`workbench ${expanded ? "desktop-expanded" : ""}`}>
      <section className="workbench-session" aria-label="任务与对话">
        <header className="session-heading">
          <div>
            <span className="workspace-kicker">SESSION</span>
            <h1>{session?.title || "开始一个新会话"}</h1>
          </div>
          <button onClick={() => onTask("")}>新建会话</button>
        </header>
        <Chat
          conversationId={conversationId || null}
          onConversation={(id) => onTask(id || "")}
          computerId={computer?.id}
          compact
        />
      </section>
      <section className="workbench-computer" aria-label="云端电脑面板">
        <div className="computer-tabs workspace-tabs">
          <button
            className={tab === "computer" ? "active" : ""}
            onClick={() => setTab("computer")}
          >
            <Monitor size={14} />
            Computer
          </button>
          <button
            className={tab === "files" ? "active" : ""}
            onClick={() => setTab("files")}
          >
            <Folder size={14} />
            Files
          </button>
          <button
            aria-label="管理电脑"
            className={tab === "settings" ? "active" : ""}
            onClick={() => setTab("settings")}
          >
            <Settings2 size={14} />
          </button>
          <button
            className="expand-desktop"
            aria-label={expanded ? "还原工作台" : "展开桌面"}
            onClick={() => setExpanded(!expanded)}
          >
            <Maximize2 size={14} />
          </button>
        </div>
        {tab === "computer" ? (
          <>
            <div className="computer-address">
              <Globe size={14} />
              <span>{String(computer?.state.url || "等待浏览器观测")}</span>
              {computer && <Badge status={computer.status} />}
            </div>
            <div className="workspace-controlbar">
              <select
                aria-label="工作台电脑"
                value={computer?.id || ""}
                onChange={(e) => {
                  setView(false);
                  select(e.target.value);
                }}
              >
                {!computer && <option value="">未注册电脑</option>}
                {computers.data?.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name}
                  </option>
                ))}
              </select>
              {computer && (
                <>
                  {view && (
                    <button onClick={() => setView(false)}>关闭桌面连接</button>
                  )}
                  {computer.control === "AGENT" && (
                    <button
                      className="primary"
                      disabled={control.isPending || controlStale}
                      onClick={() => control.mutate("take-control")}
                    >
                      <MousePointer2 size={14} />
                      接管电脑
                    </button>
                  )}
                  {computer.control === "HUMAN" && (
                    <button
                      className="primary"
                      disabled={control.isPending || controlStale}
                      onClick={() => control.mutate("hand-back")}
                    >
                      <CornerUpLeft size={14} />
                      交还 Agent
                    </button>
                  )}
                  <button
                    disabled={control.isPending || controlStale}
                    onClick={() =>
                      control.mutate(
                        computer.control === "PAUSED" ? "resume" : "pause",
                      )
                    }
                  >
                    {computer.control === "PAUSED" ? (
                      <Play size={14} />
                    ) : (
                      <Pause size={14} />
                    )}
                    {computer.control === "PAUSED" ? "恢复" : "暂停"}
                  </button>
                </>
              )}
            </div>
            <ErrorNotice error={computers.error} />
            {controlStale && (
              <p role="status">电脑控制状态待同步，暂不能接管或交还。</p>
            )}
            {task && task.computer_id !== computer?.id && (
              <p className="computer-context-note">
                当前画面不属于此任务的执行电脑
                {!task.computer_id
                  ? "：此任务尚未绑定电脑"
                  : "，请切换到任务绑定的电脑"}
                。
              </p>
            )}
            {view && computer?.control === "HUMAN" && !demo ? (
              <EmbeddedDesktop
                computerId={computer.id}
                onClose={() => setView(false)}
              />
            ) : !demo &&
              computer?.kind === "linux" &&
              computer.control !== "HUMAN" ? (
              <ComputerPreview key={computer.id} computerId={computer.id} />
            ) : demo ? (
              <div className="demo-desktop" aria-label="示例桌面画面">
                <div className="demo-browser">
                  <div className="demo-browser-tab">
                    <Globe size={14} />
                    Chromium <span>示例画面</span>
                  </div>
                  <div className="demo-browser-address">
                    {String(computer?.state.url || "https://example.com")}
                  </div>
                  <div className="demo-browser-page">
                    <span className="workspace-kicker">
                      PERSONAL AGENT COMPUTER
                    </span>
                    <h2>
                      你的目标，
                      <br />
                      在这台电脑上继续。
                    </h2>
                    <p>这里将显示云端真实桌面与浏览器。</p>
                    <div className="demo-browser-line" />
                    <div className="demo-browser-line short" />
                  </div>
                </div>
                <div className="desktop-mode">
                  <i />
                  {computer?.control === "HUMAN"
                    ? "控制权在你手中"
                    : computer?.control === "PAUSED"
                      ? "电脑已暂停"
                      : "Agent 工作中 · 示例"}
                </div>
                {computer?.control === "HUMAN" && (
                  <button disabled>打开远程桌面</button>
                )}
              </div>
            ) : (
              <div className="workspace-empty desktop-empty">
                <Monitor size={44} />
                <h2>
                  {computer?.control === "HUMAN"
                    ? "控制权在你手中"
                    : "云端桌面与浏览器"}
                </h2>
                <p>
                  {!computer
                    ? "先注册并配置你的 Linux Computer。"
                    : computer.control === "HUMAN"
                      ? "连接后，远程桌面会显示在这个面板内。"
                      : "接管后可在这里连接桌面。"}
                </p>
                {computer?.control === "HUMAN" ? (
                  <button
                    className="primary"
                    disabled={
                      !profile?.computer_url ||
                      open.isPending ||
                      controlStale ||
                      computer.kind !== "linux"
                    }
                    onClick={() => open.mutate(undefined)}
                  >
                    打开远程桌面
                  </button>
                ) : (
                  <button onClick={() => setTab("settings")}>配置电脑</button>
                )}
                {!profile?.computer_url && computer && (
                  <button
                    className="text-button"
                    onClick={() => setPage("settings")}
                  >
                    设置远程桌面地址
                  </button>
                )}
              </div>
            )}
            <div className="workspace-desktop-footer">
              <span>
                {demo
                  ? "示例画面 · 未连接真实设备"
                  : view
                    ? "远程连接已打开；连接状态以桌面画面为准"
                    : computer?.control !== "HUMAN" &&
                        computer?.kind === "linux"
                      ? "只读浏览器画面 · 每 2 秒刷新 · 接管后可操作完整桌面"
                      : "桌面未连接"}
              </span>
              {computer && <Badge status={computer.control} />}
            </div>
          </>
        ) : (
          <div className="workspace-resource-scroll">
            {tab === "files" ? <Files /> : <ComputerPage />}
          </div>
        )}
      </section>
    </div>
  );
}
