import {
  ArrowRight,
  Clock3,
  FileText,
  Monitor,
  Play,
  ShieldCheck,
  Sparkles,
} from "lucide-react";
import { api } from "../lib/api";
import { useApp, useCommand, useData } from "../lib/state";
import {
  Badge,
  ConnectionEmpty,
  Empty,
  ErrorNotice,
  Header,
  Json,
  Loading,
  time,
} from "../components/ui";
export function Approvals({
  taskId,
  disabled = false,
}: { taskId?: string; disabled?: boolean } = {}) {
  const q = useData(["approvals"], api.approvals);
  const { synchronizing, demo } = useApp();
  const unavailable = disabled || q.isError || (!demo && synchronizing);
  const pending = q.data?.filter((a) => !taskId || a.action.task_id === taskId);
  const command = useCommand(
    ({ id, approve }: { id: string; approve: boolean }) =>
      api.decide(id, approve),
    "已提交审批",
  );
  return (
    <>
      <ErrorNotice error={q.error} />
      {pending?.map((a) => (
        <div className="approval-card" key={a.id}>
          <div className="section-heading">
            <span className="approval-title">
              <ShieldCheck size={18} />
              {String(a.action.spec.type)}
            </span>
            <span className="risk">需要授权</span>
          </div>
          <p>任务 {a.action.task_id.slice(0, 8)} 提出了以下动作：</p>
          <Json data={a.action.spec} />
          <div className="button-row">
            <button
              className="primary"
              disabled={unavailable || command.isPending}
              onClick={() => command.mutate({ id: a.id, approve: true })}
            >
              批准执行
            </button>
            <button
              disabled={unavailable || command.isPending}
              onClick={() => command.mutate({ id: a.id, approve: false })}
            >
              拒绝
            </button>
          </div>
        </div>
      ))}
      {pending?.length === 0 && (
        <div className="quiet-note">
          <ShieldCheck size={17} />
          暂时没有待确认的动作
        </div>
      )}
    </>
  );
}
export function Dashboard() {
  const { connected, setPage } = useApp();
  const tasks = useData(["tasks"], api.tasks);
  const computers = useData(["computers"], api.computers);
  const events = useData(["events"], api.events);
  const approvals = useData(["approvals"], api.approvals);
  const running = tasks.data?.filter((t) => t.status === "RUNNING") || [];
  const waiting = tasks.data?.filter((t) => t.status === "WAITING_HUMAN") || [];
  return (
    <>
      <Header
        eyebrow="YOUR PERSONAL WORKSPACE"
        title="你的电脑，一直在线。"
        description="把目标交给 Agent，随时查看进度，也随时接管。"
      >
        <button className="primary" onClick={() => setPage("tasks")}>
          新建任务 <ArrowRight size={16} />
        </button>
      </Header>
      {!connected ? (
        <ConnectionEmpty />
      ) : (
        <>
          <ErrorNotice error={tasks.error} />
          <div className="metrics">
            <Metric
              icon={<Play size={19} />}
              label="正在执行"
              value={running.length}
            />
            <Metric
              icon={<ShieldCheck size={19} />}
              label="等待你确认"
              value={approvals.data?.length || waiting.length}
            />
            <Metric
              icon={<Monitor size={19} />}
              label="在线电脑"
              value={
                computers.data?.filter((c) => c.status === "ONLINE").length || 0
              }
            />
            <Metric
              icon={<FileText size={19} />}
              label="已完成任务"
              value={
                tasks.data?.filter((t) => t.status === "COMPLETED").length || 0
              }
            />
          </div>
          <div className="dashboard-grid">
            <div>
              <section className="panel">
                <div className="section-heading">
                  <h2>正在发生</h2>
                  <button
                    className="text-button"
                    onClick={() => setPage("tasks")}
                  >
                    全部任务 <ArrowRight size={14} />
                  </button>
                </div>
                {tasks.isLoading ? (
                  <Loading />
                ) : (
                  tasks.data
                    ?.filter(
                      (t) => !["COMPLETED", "CANCELLED"].includes(t.status),
                    )
                    .slice(0, 5)
                    .map((t) => (
                      <button
                        className="task-line"
                        key={t.id}
                        onClick={() => setPage("tasks")}
                      >
                        <div className="task-symbol">
                          <Sparkles size={18} />
                        </div>
                        <div className="task-line-body">
                          <strong>{t.title}</strong>
                          <span>
                            已执行 {t.current_step} 个动作 ·{" "}
                            {time(t.updated_at)}
                          </span>
                        </div>
                        <Badge status={t.status} />
                      </button>
                    ))
                )}
                {tasks.data?.length === 0 && (
                  <Empty
                    title="今天想完成什么？"
                    description="创建一个明确的目标，Agent 会在 Kernel 中持续执行。"
                  />
                )}
              </section>
              <section className="panel">
                <div className="section-heading">
                  <h2>等待你确认</h2>
                  <span className="count">{approvals.data?.length || 0}</span>
                </div>
                <Approvals />
              </section>
            </div>
            <div>
              <section className="panel computer-card">
                <div className="section-heading">
                  <h2>Cloud Computer</h2>
                  <Monitor size={20} />
                </div>
                {computers.data?.slice(0, 3).map((c) => (
                  <div className="computer-summary" key={c.id}>
                    <div className="computer-illustration">
                      <Monitor size={40} />
                      <span
                        className={
                          c.status === "ONLINE" ? "online-dot" : "offline-dot"
                        }
                      />
                    </div>
                    <h3>{c.name}</h3>
                    <div className="badge-row">
                      <Badge status={c.status} />
                      <Badge status={c.control} />
                    </div>
                    <p className="truncate">
                      {String(c.state.title || "尚未观测浏览器")}
                    </p>
                  </div>
                ))}
                {computers.data?.length === 0 && (
                  <p className="muted">
                    尚未注册电脑。设备准备好后可在 Computer 页面接入。
                  </p>
                )}
                <button
                  className="full-width"
                  onClick={() => setPage("computer")}
                >
                  打开 Computer <ArrowRight size={15} />
                </button>
              </section>
              <section className="panel">
                <div className="section-heading">
                  <h2>最近活动</h2>
                  <Clock3 size={17} />
                </div>
                <div className="timeline">
                  {events.data?.slice(0, 8).map((e) => (
                    <div className="timeline-item" key={e.id}>
                      <i />
                      <div>
                        <span>{eventLabel(e.type)}</span>
                        <small>{time(e.created_at)}</small>
                      </div>
                    </div>
                  ))}
                </div>
                {events.data?.length === 0 && (
                  <p className="muted">新的任务和动作会显示在这里。</p>
                )}
              </section>
            </div>
          </div>
        </>
      )}
    </>
  );
}
function Metric({
  icon,
  label,
  value,
}: {
  icon: React.ReactNode;
  label: string;
  value: number;
}) {
  return (
    <div className="metric">
      <div className="metric-label">
        {icon}
        {label}
      </div>
      <strong>{value.toString().padStart(2, "0")}</strong>
    </div>
  );
}
function eventLabel(type: string) {
  return (
    (
      {
        "task.created": "创建了新任务",
        "task.started": "Agent 开始执行",
        "task.completed": "任务已完成",
        "task.paused": "任务已暂停",
        "approval.requested": "动作等待你的授权",
        "action.succeeded": "动作执行成功",
        "computer.control_changed": "电脑控制权已变更",
        "model.completed": "模型完成了一轮推理",
        "memory.created": "新增了一条记忆",
      } as Record<string, string>
    )[type] || type
  );
}
