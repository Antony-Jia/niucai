import { useEffect, useState } from "react";
import {
  ListChecks,
  Monitor,
  Folder,
  Settings as SettingsIcon,
  ChevronRight,
  X,
  RefreshCw,
} from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";
import { useApp, useStale } from "./lib/state";
import { useTaskItems } from "./lib/use-task-items";
import { useLocalRunnerAutostart } from "./lib/local-runner";
import { Home } from "./pages/Home";
import { Workbench } from "./pages/Workbench";
import { StandaloneTask } from "./pages/StandaloneTask";
import { Files, MemoryPage } from "./pages/FilesMemory";
import { Settings } from "./pages/Settings";
import { RemotePi } from "./pages/RemotePi";
import { Kanban } from "./pages/Kanban";
import { Whiteboard } from "./pages/Whiteboard";
import { Goals } from "./pages/Goals";
import { Bots } from "./pages/Bots";
import { Extensions } from "./pages/Extensions";
import { DevicesPage } from "./pages/DevicesPage";
import type { Page } from "./lib/types";

type Section = "tasks" | "files" | "devices" | "settings";

/** 主导航只有三项加设置；旧页面保留，但归入对应分区而不再占一级入口。 */
const navigation: {
  id: Section;
  page: Page;
  label: string;
  icon: typeof Monitor;
}[] = [
  { id: "tasks", page: "home", label: "任务", icon: ListChecks },
  { id: "files", page: "files", label: "文件", icon: Folder },
  { id: "devices", page: "devices", label: "设备", icon: Monitor },
];

const sectionOf: Record<Page, Section> = {
  home: "tasks",
  dashboard: "tasks",
  computer: "tasks",
  chat: "tasks",
  tasks: "tasks",
  task: "tasks",
  goals: "tasks",
  files: "files",
  devices: "devices",
  remote: "devices",
  settings: "settings",
  bots: "settings",
  extensions: "settings",
  memory: "settings",
  board: "settings",
  kanban: "settings",
};

const sectionLabel: Record<Section, string> = {
  tasks: "任务",
  files: "文件",
  devices: "设备",
  settings: "设置",
};

const subLabel: Partial<Record<Page, string>> = {
  computer: "会话",
  chat: "会话",
  tasks: "会话",
  task: "任务详情",
  goals: "任务详情",
  remote: "远程 Pi",
  bots: "助手",
  extensions: "技能与工具",
  memory: "偏好与记忆",
  board: "白板",
  kanban: "看板",
};

export function App() {
  const {
    page,
    setPage,
    connected,
    demo,
    profile,
    stream,
    retryConnection,
    notice,
    notify,
  } = useApp();
  const cache = useQueryClient();
  useLocalRunnerAutostart(connected && !demo, notify);
  const { counts, stale: taskStale } = useTaskItems();
  const stale = useStale(taskStale);
  const [workspaceTask, setWorkspaceTask] = useState<string | null>(null);
  const [standaloneId, setStandaloneId] = useState<string | null>(null);
  const [goalId, setGoalId] = useState<string | null>(null);
  const [focusGoal, setFocusGoal] = useState<string | null>(null);
  const section = sectionOf[page];
  useEffect(() => {
    function shortcut(e: KeyboardEvent) {
      if (
        e.ctrlKey &&
        !e.altKey &&
        !e.metaKey &&
        /^[1-3]$/.test(e.key) &&
        !(e.target instanceof HTMLInputElement) &&
        !(e.target instanceof HTMLTextAreaElement)
      ) {
        e.preventDefault();
        setPage(navigation[Number(e.key) - 1].page);
      }
    }
    window.addEventListener("keydown", shortcut);
    return () => window.removeEventListener("keydown", shortcut);
  }, [setPage]);

  function content() {
    switch (page) {
      case "home":
      case "dashboard":
        return (
          <Home
            onTask={setWorkspaceTask}
            onStandalone={setStandaloneId}
            onGoal={setGoalId}
            focusGoal={focusGoal}
            onFocusGoal={setFocusGoal}
          />
        );
      case "chat":
      case "tasks":
      case "computer":
        return <Workbench taskId={workspaceTask} onTask={setWorkspaceTask} />;
      case "task":
        return <StandaloneTask taskId={standaloneId} />;
      case "goals":
        return (
          <Goals
            goalId={goalId}
            onGoal={setGoalId}
            onMap={(id) => {
              setFocusGoal(id);
              setPage("home");
            }}
            onBack={() => setPage("home")}
          />
        );
      case "files":
        return <Files />;
      case "devices":
        return <DevicesPage />;
      case "remote":
        return <RemotePi />;
      case "settings":
        return <Settings onTask={setWorkspaceTask} />;
      case "bots":
        return <Bots />;
      case "extensions":
        return <Extensions />;
      case "memory":
        return <MemoryPage />;
      case "board":
        return <Whiteboard />;
      case "kanban":
        return <Kanban />;
    }
  }

  return (
    <div className="app-shell desktop-app">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">
            n<span />
          </div>
          <div>
            niucai<small>PERSONAL AGENT COMPUTER</small>
          </div>
        </div>
        <nav aria-label="主导航">
          {navigation.map(({ id, page: target, label, icon: Icon }, i) => (
            <button
              key={id}
              aria-current={section === id ? "page" : undefined}
              // 名称固定为入口名：窄屏隐藏文字或出现徽标时也不变，徽标数量作补充说明。
              aria-label={label}
              aria-describedby={
                id === "tasks" && counts.tasks > 0
                  ? "nav-tasks-badge"
                  : undefined
              }
              className={section === id ? "nav-item active" : "nav-item"}
              title={`${label}（Ctrl+${i + 1}）`}
              onClick={() => setPage(target)}
            >
              <Icon size={18} />
              <span>{label}</span>
              {id === "tasks" && counts.tasks > 0 && (
                <>
                  <em className="nav-badge" aria-hidden="true">
                    {counts.tasks}
                  </em>
                  <span id="nav-tasks-badge" className="sr-only">
                    {counts.tasks} 项任务需要你处理
                  </span>
                </>
              )}
            </button>
          ))}
        </nav>
        <div className="sidebar-spacer" />
        <button
          aria-current={section === "settings" ? "page" : undefined}
          className={
            section === "settings"
              ? "nav-item active settings-nav"
              : "nav-item settings-nav"
          }
          aria-label="设置"
          title="设置"
          onClick={() => setPage("settings")}
        >
          <SettingsIcon size={18} />
          <span>设置</span>
        </button>
        <button
          className="sidebar-bottom"
          onClick={() => setPage("settings")}
          title={
            demo
              ? "数据仅用于体验"
              : profile?.name || profile?.base_url || "打开设置以连接 Kernel"
          }
        >
          <span className="connection-dot">
            <i className={connected && !stale ? "connected" : ""} />
            <strong>
              {demo
                ? "示例工作台"
                : connected
                  ? stale
                    ? "Kernel 待同步"
                    : "Kernel 已连接"
                  : "Kernel 未连接"}
            </strong>
          </span>
          <ChevronRight size={14} />
        </button>
      </aside>
      <main className={`main ${page === "computer" ? "workbench-layout" : ""}`}>
        <div className="topbar">
          <span>
            Personal workspace <ChevronRight size={13} />
            {subLabel[page] ? (
              <>
                {sectionLabel[section]} <ChevronRight size={13} />
                <strong>{subLabel[page]}</strong>
              </>
            ) : (
              <strong>{sectionLabel[section]}</strong>
            )}
          </span>
          <div>
            <span className="sync-status">
              <i className={connected && !stale ? "connected" : ""} />
              {stale ? "状态待同步" : stream}
            </span>
            <button
              className="icon-button"
              aria-label="刷新"
              title="刷新当前 Kernel 数据"
              disabled={!connected}
              onClick={() =>
                stale ? void retryConnection() : void cache.invalidateQueries()
              }
            >
              <RefreshCw size={16} />
            </button>
            <div className="avatar">YOU</div>
          </div>
        </div>
        {demo && (
          <div className="demo-banner">
            示例模式 · 不连接真实模型或设备
            <button onClick={() => setPage("settings")}>
              连接真实 Kernel <ChevronRight size={13} />
            </button>
          </div>
        )}
        {stale && (
          <div className="connection-banner" role="status">
            <div>
              <strong>Kernel 连接待恢复</strong>
              <p>
                {profile?.name || "当前连接"} ·{" "}
                {profile?.base_url || "地址待配置"}
              </p>
              <small>当前显示最后已知状态，服务器上的任务可能仍在执行。</small>
            </div>
            <div className="button-row">
              <button onClick={() => void retryConnection()}>重新连接</button>
              <button onClick={() => setPage("settings")}>检查连接设置</button>
            </div>
          </div>
        )}
        <div className="page-content">{content()}</div>
        <footer>
          <span>YOUR KERNEL. YOUR COMPUTER.</span>
          <span>所有任务与状态由 Kernel 持久保存</span>
        </footer>
      </main>
      {notice && (
        <div className="toast" role="status">
          <span>{notice}</span>
          <button aria-label="关闭通知" onClick={() => notify("")}>
            <X size={15} />
          </button>
        </div>
      )}
    </div>
  );
}
