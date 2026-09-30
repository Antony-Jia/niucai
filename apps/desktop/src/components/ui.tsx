import { useEffect, useRef, type ReactNode } from "react";
import { ArrowRight, Circle, LoaderCircle, X } from "lucide-react";
import { useApp } from "../lib/state";
import type { TaskStatus } from "../lib/types";
export const statuses: Record<TaskStatus, string> = {
  PENDING: "等待执行",
  RUNNING: "执行中",
  WAITING_HUMAN: "等待你确认",
  PAUSED: "已暂停",
  COMPLETED: "已完成",
  FAILED: "执行失败",
  CANCELLED: "已取消",
};
export function Badge({ status }: { status: string }) {
  return (
    <span className={`badge ${status.toLowerCase()}`}>
      <Circle size={6} fill="currentColor" />
      {statuses[status as TaskStatus] ||
        (
          {
            AGENT: "Agent 控制",
            HUMAN: "你正在控制",
            ONLINE: "在线",
            OFFLINE: "离线",
          } as Record<string, string>
        )[status] ||
        status}
    </span>
  );
}
export function Header({
  eyebrow,
  title,
  description,
  children,
}: {
  eyebrow: string;
  title: string;
  description: string;
  children?: ReactNode;
}) {
  return (
    <div className="page-heading">
      <div>
        <div className="eyebrow">{eyebrow}</div>
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
      <div className="heading-actions">{children}</div>
    </div>
  );
}
export function Empty({
  title,
  description,
  children,
}: {
  title: string;
  description: string;
  children?: ReactNode;
}) {
  return (
    <div className="empty">
      <span className="empty-orbit">
        <Circle size={25} />
      </span>
      <h3>{title}</h3>
      <p>{description}</p>
      {children}
    </div>
  );
}
export function ConnectionEmpty() {
  const { setPage, explore } = useApp();
  return (
    <Empty
      title="先连接你的 Personal Computer"
      description="填入 Kernel 地址和连接凭据，即可查看任务、记忆和电脑状态。"
    >
      <div className="button-row">
        <button className="primary" onClick={() => setPage("settings")}>
          配置连接 <ArrowRight size={16} />
        </button>
        <button onClick={explore}>体验示例</button>
      </div>
    </Empty>
  );
}
export function Loading() {
  return (
    <div className="loading">
      <LoaderCircle className="spin" size={19} />
      正在加载…
    </div>
  );
}
export function ErrorNotice({ error }: { error: unknown }) {
  return error ? (
    <div className="error-notice">
      {error instanceof Error ? error.message : String(error)}
    </div>
  ) : null;
}
export function time(value: string) {
  const date = new Date(value.endsWith("Z") ? value : value + "Z");
  return isNaN(date.getTime())
    ? value
    : new Intl.DateTimeFormat("zh-CN", {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      }).format(date);
}
export function Modal({
  title,
  onClose,
  children,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
}) {
  const dialog = useRef<HTMLElement>(null);
  const dismiss = useRef(onClose);
  dismiss.current = onClose;
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const focusable = () =>
      Array.from(
        dialog.current?.querySelectorAll<HTMLElement>(
          'button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled), [tabindex="0"]',
        ) || [],
      );
    focusable()[0]?.focus();
    function key(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        dismiss.current();
      }
      if (event.key === "Tab") {
        const list = focusable(),
          first = list[0],
          last = list[list.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last?.focus();
        }
        if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first?.focus();
        }
      }
    }
    document.addEventListener("keydown", key);
    return () => {
      document.removeEventListener("keydown", key);
      previous?.focus();
    };
  }, []);
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <section
        ref={dialog}
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="section-heading">
          <h2>{title}</h2>
          <button className="icon-button" aria-label="关闭" onClick={onClose}>
            <X size={18} />
          </button>
        </div>
        {children}
      </section>
    </div>
  );
}
export function Json({ data }: { data: unknown }) {
  return <pre className="json">{JSON.stringify(data, null, 2)}</pre>;
}
