import { useEffect, useRef, useState } from "react";
import {
  Monitor,
  MousePointer2,
  CornerUpLeft,
  Pause,
  Play,
  Maximize2,
  ArrowUp,
  Globe,
  Folder,
  Settings2,
} from "lucide-react";
import { api, closeComputer, embedComputer, native } from "../lib/api";
import { useApp, useCommand, useData } from "../lib/state";
import { Badge, ConnectionEmpty, ErrorNotice } from "../components/ui";
import { TaskDetail } from "./Tasks";
import { Chat } from "./Chat";
import { Files } from "./FilesMemory";
import { ComputerPage } from "./Computer";
import { Approvals } from "./Dashboard";

export function Workbench({
  taskId,
  onTask,
}: {
  taskId: string | null;
  onTask: (id: string) => void;
}) {
  const { connected, demo, profile, setPage, notify } = useApp();
  const tasks = useData(["tasks"], api.tasks);
  const computers = useData(["computers"], api.computers);
  const task = tasks.data?.find((t) => t.id === taskId) || tasks.data?.[0];
  const [selected, select] = useState("");
  const computer =
    computers.data?.find((c) => c.id === (selected || task?.computer_id)) ||
    computers.data?.[0];
  const [tab, setTab] = useState("computer");
  const [conversation, setConversation] = useState(false);
  const [goal, setGoal] = useState("");
  const [view, setView] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const create = useCommand(
    (text: string) => api.createTask(text.slice(0, 70), text, computer?.id),
    "任务已创建",
  );
  const control = useCommand(async (operation: string) => {
    setView(false);
    await closeComputer();
    if (!computer) throw new Error("请选择电脑");
    return api.computerControl(computer.id, operation);
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
            <h1>{task?.title || "开始一个新目标"}</h1>
          </div>
          {task && <Badge status={task.status} />}
        </header>
        <div className="workspace-tabs">
          <button
            className={!conversation ? "active" : ""}
            onClick={() => setConversation(false)}
          >
            执行过程
          </button>
          <button
            className={conversation ? "active" : ""}
            onClick={() => setConversation(true)}
          >
            对话
          </button>
        </div>
        <div className="session-scroll">
          {conversation ? (
            <Chat />
          ) : (
            <>
              <ErrorNotice error={tasks.error} />
              {task ? (
                <TaskDetail task={task} />
              ) : (
                <div className="workspace-empty">
                  <h2>你想完成什么？</h2>
                  <p>描述目标，选择云端电脑，然后交给 Agent。</p>
                </div>
              )}
              <section className="workspace-approvals">
                <h2>等待你的决定</h2>
                <Approvals />
              </section>
            </>
          )}
        </div>
        {!conversation && (
          <form
            className="workspace-composer"
            onSubmit={(e) => {
              e.preventDefault();
              const text = goal.trim();
              if (text)
                create.mutate(text, {
                  onSuccess: (t) => {
                    onTask(t.id);
                    setGoal("");
                  },
                });
            }}
          >
            <textarea
              aria-label="工作台任务目标"
              placeholder="让 Agent 完成一件事…"
              value={goal}
              onChange={(e) => setGoal(e.target.value)}
              maxLength={16000}
              required
            />
            <div>
              <span>{computer ? computer.name : "纯推理任务"}</span>
              <button
                className="primary"
                aria-label="创建工作台任务"
                disabled={!goal.trim() || create.isPending}
              >
                <ArrowUp size={16} />
              </button>
            </div>
          </form>
        )}
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
                  {computer.control === "AGENT" && (
                    <button
                      className="primary"
                      disabled={control.isPending}
                      onClick={() => control.mutate("take-control")}
                    >
                      <MousePointer2 size={14} />
                      接管电脑
                    </button>
                  )}
                  {computer.control === "HUMAN" && (
                    <button
                      className="primary"
                      disabled={control.isPending}
                      onClick={() => control.mutate("hand-back")}
                    >
                      <CornerUpLeft size={14} />
                      交还 Agent
                    </button>
                  )}
                  <button
                    disabled={control.isPending}
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
            {view && computer?.control === "HUMAN" && !demo ? (
              <EmbeddedDesktop computerId={computer.id} />
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
                      : "接管后可在这里连接桌面。Agent 运行时的实时只读画面尚未接入。"}
                </p>
                {computer?.control === "HUMAN" ? (
                  <button
                    className="primary"
                    disabled={
                      !profile?.computer_url ||
                      open.isPending ||
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

function EmbeddedDesktop({ computerId }: { computerId: string }) {
  const element = useRef<HTMLDivElement>(null);
  const { notify } = useApp();
  useEffect(() => {
    let stopped = false;
    let previous = "";
    let timer: ReturnType<typeof setTimeout> | undefined;
    const update = () => {
      if (timer) clearTimeout(timer);
      timer = setTimeout(() => {
        const rect = element.current?.getBoundingClientRect();
        if (stopped || !rect) return;
        const bounds = {
          x: rect.x,
          y: rect.y,
          width: rect.width,
          height: rect.height,
          visible: !document.querySelector(".modal-backdrop, .toast"),
        };
        const key = JSON.stringify(bounds);
        if (key === previous) return;
        previous = key;
        void embedComputer(computerId, bounds).catch((e) => {
          if (!stopped) notify(String(e));
        });
      }, 60);
    };
    const resize = new ResizeObserver(update);
    const overlays = new MutationObserver(update);
    if (element.current) resize.observe(element.current);
    overlays.observe(document.body, { childList: true, subtree: true });
    window.addEventListener("resize", update);
    update();
    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
      resize.disconnect();
      overlays.disconnect();
      window.removeEventListener("resize", update);
      void closeComputer();
    };
  }, [computerId, notify]);
  return (
    <div
      className="embedded-desktop"
      ref={element}
      aria-label="远程桌面连接区域"
    >
      <span>正在连接云端桌面…</span>
    </div>
  );
}
