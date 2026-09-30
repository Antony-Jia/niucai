import { useEffect, useRef, useState } from "react";
import {
  Home,
  MessageSquare,
  ListTodo,
  Monitor,
  ShieldCheck,
  Menu,
  X,
} from "lucide-react";
import { useApp, useData } from "../../desktop/src/lib/state";
import { api } from "../../desktop/src/lib/api";
import { Dashboard, Approvals } from "../../desktop/src/pages/Dashboard";
import { Chat } from "../../desktop/src/pages/Chat";
import { Tasks } from "../../desktop/src/pages/Tasks";
import { Files, MemoryPage } from "../../desktop/src/pages/FilesMemory";
import { Modal } from "../../desktop/src/components/ui";
import { Computer } from "./Computer";
import { Settings } from "./Settings";
import { useEvents } from "./events";
import type { Page } from "../../desktop/src/lib/types";
const nav = [
  { page: "dashboard", label: "首页", Icon: Home },
  { page: "chat", label: "对话", Icon: MessageSquare },
  { page: "tasks", label: "任务", Icon: ListTodo },
  { page: "computer", label: "电脑", Icon: Monitor },
  { page: "approvals", label: "审批", Icon: ShieldCheck },
] as const;
export function MobileApp() {
  const { page, setPage, demo, connected, notice, notify } = useApp();
  const [approvalsPage, setApprovalsPage] = useState(false);
  const [more, setMore] = useState(false);
  const [online, setOnline] = useState(navigator.onLine);
  const status = useEvents();
  const approvals = useData(["approvals"], api.approvals);
  const Component = {
    dashboard: Dashboard,
    chat: Chat,
    tasks: Tasks,
    computer: Computer,
    files: Files,
    memory: MemoryPage,
    settings: Settings,
  }[page];
  useEffect(() => {
    function update() {
      setOnline(navigator.onLine);
    }
    window.addEventListener("online", update);
    window.addEventListener("offline", update);
    function resize() {
      document.documentElement.style.setProperty(
        "--app-height",
        `${window.visualViewport?.height || innerHeight}px`,
      );
    }
    resize();
    window.visualViewport?.addEventListener("resize", resize);
    return () => {
      window.removeEventListener("online", update);
      window.removeEventListener("offline", update);
      window.visualViewport?.removeEventListener("resize", resize);
    };
  }, []);
  const previousApprovals = useRef<Set<string> | null>(null);
  useEffect(() => {
    if (!approvals.data) return;
    const ids = new Set(approvals.data.map((a) => a.id));
    if (
      previousApprovals.current &&
      [...ids].some((id) => !previousApprovals.current?.has(id)) &&
      "Notification" in window &&
      Notification.permission === "granted"
    )
      void navigator.serviceWorker
        ?.getRegistration()
        .then((r) =>
          r?.showNotification("niucai · 等待你审批", {
            body: `有 ${ids.size} 项待审批动作`,
            icon: "/icons/icon-192.png",
            tag: "niucai-approval",
          }),
        )
        .catch(() => {});
    previousApprovals.current = ids;
  }, [approvals.data]);
  function go(p: Page) {
    setApprovalsPage(false);
    setPage(p);
    setMore(false);
    window.scrollTo({ top: 0 });
  }
  return (
    <div className="mobile-app">
      <header className="mobile-header">
        <button className="mobile-brand" onClick={() => go("dashboard")}>
          n<span>niucai</span>
        </button>
        <span className="mobile-status">{online ? status : "离线"}</span>
        <button aria-label="更多页面" onClick={() => setMore(true)}>
          <Menu size={21} />
        </button>
      </header>
      {demo && (
        <div className="demo-banner">
          示例模式 · 不连接真实模型或设备
          <button onClick={() => go("settings")}>连接</button>
        </div>
      )}
      {!online && (
        <div className="offline-banner">
          当前离线。服务器任务仍保持原状态，联网后同步。
        </div>
      )}
      <main className="mobile-content">
        {approvalsPage ? (
          <>
            <div className="page-heading">
              <h1>等待你确认</h1>
              <p>查看动作内容，再决定批准或拒绝。</p>
            </div>
            {connected ? (
              <section className="panel">
                <Approvals />
              </section>
            ) : (
              <>
                <p>请先登录 Kernel。</p>
                <button className="primary" onClick={() => go("settings")}>
                  手机登录
                </button>
              </>
            )}
          </>
        ) : (
          <Component />
        )}
      </main>
      <nav className="mobile-nav" aria-label="手机导航">
        {nav.map(({ page: p, label, Icon }) => (
          <button
            key={p}
            aria-label={label}
            aria-current={
              (approvalsPage ? p === "approvals" : p === page)
                ? "page"
                : undefined
            }
            onClick={() => {
              if (p === "approvals") {
                setApprovalsPage(true);
                window.scrollTo({ top: 0 });
              } else go(p);
            }}
          >
            <span>
              <Icon size={21} />
              {p === "approvals" && !!approvals.data?.length && (
                <b>{approvals.data.length}</b>
              )}
            </span>
            {label}
          </button>
        ))}
      </nav>
      {more && (
        <Modal title="工作台" onClose={() => setMore(false)}>
          <div className="mobile-menu">
            <button onClick={() => go("files")}>文件与下载</button>
            <button onClick={() => go("memory")}>记忆</button>
            <button onClick={() => go("settings")}>登录与安装设置</button>
          </div>
        </Modal>
      )}
      {notice && (
        <div className="toast" role="status">
          <span>{notice}</span>
          <button aria-label="关闭通知" onClick={() => notify("")}>
            <X size={18} />
          </button>
        </div>
      )}
    </div>
  );
}
