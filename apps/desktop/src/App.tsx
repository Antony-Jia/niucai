import { useEffect, useState } from "react";
import {
  LayoutDashboard,
  MessageSquare,
  ListTodo,
  Monitor,
  Folder,
  Brain,
  Settings as SettingsIcon,
  ChevronRight,
  X,
  RefreshCw,
} from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";
import { useApp, useData } from "./lib/state";
import { Dashboard } from "./pages/Dashboard";
import { Tasks } from "./pages/Tasks";
import { Chat } from "./pages/Chat";
import { Workbench } from "./pages/Workbench";
import { api } from "./lib/api";
import { Files, MemoryPage } from "./pages/FilesMemory";
import { Settings } from "./pages/Settings";
import type { Page } from "./lib/types";
const navigation: { id: Page; label: string; icon: typeof Monitor }[] = [
  { id: "dashboard", label: "Dashboard", icon: LayoutDashboard },
  { id: "chat", label: "Chat", icon: MessageSquare },
  { id: "tasks", label: "Tasks", icon: ListTodo },
  { id: "computer", label: "Computer", icon: Monitor },
  { id: "files", label: "Files", icon: Folder },
  { id: "memory", label: "Memory", icon: Brain },
  { id: "settings", label: "Settings", icon: SettingsIcon },
];
export function App() {
  const { page, setPage, connected, demo, profile, stream, notice, notify } =
    useApp();
  const cache = useQueryClient();
  const tasks = useData(["tasks"], api.tasks);
  const [workspaceTask, setWorkspaceTask] = useState<string | null>(null);
  useEffect(() => {
    function shortcut(e: KeyboardEvent) {
      if (
        e.ctrlKey &&
        !e.altKey &&
        !e.metaKey &&
        /^[1-7]$/.test(e.key) &&
        !(e.target instanceof HTMLInputElement) &&
        !(e.target instanceof HTMLTextAreaElement)
      ) {
        e.preventDefault();
        setPage(navigation[Number(e.key) - 1].id);
      }
    }
    window.addEventListener("keydown", shortcut);
    return () => window.removeEventListener("keydown", shortcut);
  }, [setPage]);
  const Current = {
    dashboard: Dashboard,
    chat: Chat,
    tasks: Tasks,
    computer: Workbench,
    files: Files,
    memory: MemoryPage,
    settings: Settings,
  }[page];
  return (
    <div
      className={`app-shell desktop-app ${page === "computer" ? "workbench-mode" : ""}`}
    >
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">
            n<span />
          </div>
          <div>
            niucai<small>PERSONAL AGENT COMPUTER</small>
          </div>
        </div>
        <div className="sidebar-section">WORKSPACE</div>
        <nav aria-label="主导航">
          {navigation.map(({ id, label, icon: Icon }, i) => (
            <button
              key={id}
              aria-current={page === id ? "page" : undefined}
              className={page === id ? "nav-item active" : "nav-item"}
              onClick={() => setPage(id)}
            >
              <Icon size={18} />
              <span>{label}</span>
              <small>{i + 1}</small>
            </button>
          ))}
        </nav>
        <div className="recent-sessions">
          <div className="sidebar-section">RECENT TASKS</div>
          {tasks.data?.slice(0, 8).map((t) => (
            <button
              key={t.id}
              className={
                (workspaceTask || tasks.data?.[0]?.id) === t.id &&
                page === "computer"
                  ? "recent-session selected"
                  : "recent-session"
              }
              onClick={() => {
                setWorkspaceTask(t.id);
                setPage("computer");
              }}
            >
              <span>{t.title}</span>
              <small>
                {t.status === "RUNNING"
                  ? "● 运行中"
                  : t.status === "WAITING_HUMAN"
                    ? "等待你"
                    : t.status}
              </small>
            </button>
          ))}
        </div>
        <div className="sidebar-bottom">
          <div className="connection-dot">
            <i className={connected ? "connected" : ""} />
            <strong>
              {demo
                ? "示例工作台"
                : connected
                  ? "Kernel 已连接"
                  : "Kernel 未连接"}
            </strong>
          </div>
          <p>
            {demo
              ? "数据仅用于体验"
              : profile?.base_url || "准备好时，连接你的电脑。"}
          </p>
          <button className="text-button" onClick={() => setPage("settings")}>
            连接设置 <ChevronRight size={14} />
          </button>
        </div>
      </aside>
      <main className="main">
        <div className="topbar">
          <span>
            Personal workspace <ChevronRight size={13} />
            <strong>{navigation.find((n) => n.id === page)?.label}</strong>
          </span>
          <div>
            <span className="sync-status">
              <i className={connected ? "connected" : ""} />
              {stream}
            </span>
            <button
              className="icon-button"
              aria-label="刷新"
              disabled={!connected}
              onClick={() => void cache.invalidateQueries()}
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
        <div className="page-content">
          <Current taskId={workspaceTask} onTask={setWorkspaceTask} />
        </div>
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
