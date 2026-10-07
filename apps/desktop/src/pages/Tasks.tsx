import { useState } from "react";
import {
  Plus,
  Pause,
  Play,
  RotateCcw,
  Square,
  Search,
  ChevronRight,
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
  Modal,
  statuses,
  time,
} from "../components/ui";
import type { Task, TaskStatus } from "../lib/types";
import { Approvals } from "./Dashboard";
export function Tasks() {
  const { connected } = useApp();
  const q = useData(["tasks"], api.tasks);
  const computers = useData(["computers"], api.computers);
  const [selected, setSelected] = useState<string | null>(null);
  const [create, setCreate] = useState(false);
  const [title, setTitle] = useState("");
  const [goal, setGoal] = useState("");
  const [computerId, setComputerId] = useState("");
  const [filter, setFilter] = useState("ALL");
  const [search, setSearch] = useState("");
  const command = useCommand(
    () => api.createTask(title, goal, computerId),
    "任务已创建",
  );
  const chosen = q.data?.find((t) => t.id === selected);
  const filtered = q.data?.filter(
    (t) =>
      (filter === "ALL" || t.status === filter) &&
      `${t.title} ${t.goal}`.toLowerCase().includes(search.toLowerCase()),
  );
  return (
    <>
      <Header
        eyebrow="TASK CONTROL"
        title="目标在这里，持续推进。"
        description="任务状态保存在 Kernel，关闭客户端也不会中断 Agent。"
      >
        <button
          className="primary"
          disabled={!connected}
          onClick={() => setCreate(true)}
        >
          <Plus size={16} />
          新建任务
        </button>
      </Header>
      {!connected ? (
        <ConnectionEmpty />
      ) : (
        <>
          <div className="toolbar">
            <div className="search">
              <Search size={17} />
              <input
                aria-label="搜索任务"
                placeholder="搜索任务…"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
            </div>
            <select
              aria-label="任务状态"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
            >
              <option value="ALL">全部状态</option>
              {Object.entries(statuses).map(([k, v]) => (
                <option key={k} value={k}>
                  {v}
                </option>
              ))}
            </select>
          </div>
          <ErrorNotice error={q.error} />
          {q.isLoading ? (
            <Loading />
          ) : (
            <div className={`tasks-layout ${chosen ? "with-detail" : ""}`}>
              <section className="panel task-list">
                {filtered?.map((t) => (
                  <button
                    className={`task-row ${selected === t.id ? "selected" : ""}`}
                    key={t.id}
                    onClick={() => setSelected(t.id)}
                  >
                    <div className="task-row-top">
                      <strong>{t.title}</strong>
                      <Badge status={t.status} />
                    </div>
                    <p>{t.goal}</p>
                    <div className="task-row-meta">
                      <span>
                        {time(t.updated_at)} · {t.current_step} 个动作
                      </span>
                      <ChevronRight size={15} />
                    </div>
                  </button>
                ))}
                {filtered?.length === 0 && (
                  <Empty
                    title="没有找到任务"
                    description="新建一个目标，或调整筛选条件。"
                  />
                )}
              </section>
              {chosen && <TaskDetail task={chosen} />}
            </div>
          )}
        </>
      )}
      {create && (
        <Modal title="创建新任务" onClose={() => setCreate(false)}>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              command.mutate(undefined, {
                onSuccess: (t) => {
                  setCreate(false);
                  setSelected(t.id);
                  setTitle("");
                  setGoal("");
                },
              });
            }}
          >
            <label>
              任务名称
              <input
                required
                maxLength={200}
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                placeholder="例如：比较三个 Agent Harness"
              />
            </label>
            <label>
              目标
              <textarea
                required
                maxLength={16000}
                rows={6}
                value={goal}
                onChange={(e) => setGoal(e.target.value)}
                placeholder="描述你希望得到的结果、范围和约束。"
              />
            </label>
            <label>
              执行电脑
              <select
                value={computerId}
                onChange={(e) => setComputerId(e.target.value)}
              >
                <option value="">不使用电脑（纯推理任务）</option>
                {computers.data
                  ?.filter((c) => c.kind === "linux")
                  .map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.name} · {c.status === "ONLINE" ? "在线" : "待连接"}
                    </option>
                  ))}
              </select>
            </label>
            <button className="primary full-width" disabled={command.isPending}>
              {command.isPending ? "正在创建…" : "交给 Agent 执行"}
            </button>
          </form>
        </Modal>
      )}
    </>
  );
}
export function TaskDetail({ task }: { task: Task }) {
  const actions = useData(["actions", task.id], () => api.actions(task.id));
  const computers = useData(["computers"], api.computers);
  const [computerId, setComputerId] = useState("");
  const assign = useCommand(
    () =>
      api.attachTaskComputer(
        task.id,
        computerId || computers.data?.find((c) => c.kind === "linux")?.id || "",
      ),
    "执行电脑已绑定，请继续任务",
  );
  const command = useCommand(
    (operation: string) => api.taskControl(task.id, operation),
    "任务状态已更新",
  );
  const reconcile = useCommand(
    ({ id, outcome }: { id: string; outcome: "SUCCEEDED" | "FAILED" }) =>
      api.reconcile(id, outcome),
    "已记录确认结果，请恢复任务",
  );
  const [cancel, setCancel] = useState(false);
  const status = task.status as TaskStatus;
  return (
    <aside className="panel task-detail">
      <div className="section-heading">
        <h2>任务详情</h2>
        <Badge status={status} />
      </div>
      <h3>{task.title}</h3>
      <p className="preserve">{task.goal}</p>
      {status === "PENDING" && (
        <p className="quiet-note">
          任务已排队，将自动开始；电脑暂停或被接管时会等待控制权交还。
        </p>
      )}
      {status === "RUNNING" && (
        <p className="quiet-note">
          Agent 正在执行
          {!actions.data?.length
            ? "，等待模型产生首个动作"
            : `，已记录 ${actions.data.length} 个动作`}
          。可暂停或取消任务。
        </p>
      )}
      {!task.computer_id &&
        !["COMPLETED", "CANCELLED", "FAILED"].includes(status) && (
          <div className="task-computer-choice">
            <p className="muted">
              此任务尚未绑定电脑；浏览器任务需要先选择执行电脑。
            </p>
            {["PENDING", "PAUSED", "WAITING_HUMAN"].includes(status) ? (
              <>
                <select
                  aria-label="任务执行电脑"
                  value={
                    computerId ||
                    computers.data?.find((c) => c.kind === "linux")?.id ||
                    ""
                  }
                  onChange={(e) => setComputerId(e.target.value)}
                >
                  {!computers.data?.some((c) => c.kind === "linux") && (
                    <option value="">尚无可用电脑</option>
                  )}
                  {computers.data
                    ?.filter((c) => c.kind === "linux")
                    .map((c) => (
                      <option value={c.id} key={c.id}>
                        {c.name}
                      </option>
                    ))}
                </select>
                <button
                  disabled={
                    assign.isPending ||
                    !computers.data?.some((c) => c.kind === "linux")
                  }
                  onClick={() => assign.mutate(undefined)}
                >
                  绑定执行电脑
                </button>
              </>
            ) : (
              <p className="muted">
                若要使用浏览器，请暂停任务后绑定电脑，再继续。
              </p>
            )}
          </div>
        )}
      <div className="button-row">
        {["PENDING", "RUNNING", "WAITING_HUMAN"].includes(status) && (
          <button
            disabled={command.isPending}
            onClick={() => command.mutate("pause")}
          >
            <Pause size={14} />
            暂停
          </button>
        )}
        {["PAUSED", "WAITING_HUMAN"].includes(status) && (
          <button
            className="primary"
            disabled={command.isPending}
            onClick={() => command.mutate("resume")}
          >
            <Play size={14} />
            恢复
          </button>
        )}
        {status === "FAILED" && (
          <button
            disabled={command.isPending}
            onClick={() => command.mutate("retry")}
          >
            <RotateCcw size={14} />
            重试
          </button>
        )}
        {!["COMPLETED", "CANCELLED"].includes(status) && (
          <button
            className="danger"
            disabled={command.isPending}
            onClick={() => setCancel(true)}
          >
            <Square size={13} />
            停止任务
          </button>
        )}
      </div>
      {task.checkpoint.result && (
        <div className="result">
          <h4>执行结果</h4>
          <p className="preserve">{task.checkpoint.result}</p>
        </div>
      )}
      {!["COMPLETED", "CANCELLED"].includes(status) &&
        task.checkpoint.reason && (
          <div className="error-notice">{task.checkpoint.reason}</div>
        )}
      {!["COMPLETED", "CANCELLED"].includes(status) &&
        task.checkpoint.detail && (
          <div className="error-notice">{task.checkpoint.detail}</div>
        )}
      {!["COMPLETED", "CANCELLED"].includes(status) && (
        <section aria-label="当前任务审批">
          <h4>待确认动作</h4>
          <Approvals taskId={task.id} />
        </section>
      )}
      <h4>任务计划</h4>
      <div className="plan">
        {task.plan.steps?.map((s, i) => (
          <div key={s.id}>
            <span>{String(i + 1).padStart(2, "0")}</span>
            <p>{s.description}</p>
          </div>
        ))}
      </div>
      <h4>动作记录</h4>
      <ErrorNotice error={actions.error} />
      {actions.data?.map((a) => (
        <details key={a.id}>
          <summary>
            {String(a.spec.type)} <span>{a.status}</span>
          </summary>
          <Json data={{ spec: a.spec, result: a.result }} />
          {a.status === "UNKNOWN" && (
            <div className="reconcile">
              <p>请先查看设备并确认此动作的实际结果。</p>
              <div className="button-row">
                <button
                  disabled={reconcile.isPending}
                  onClick={() =>
                    reconcile.mutate({ id: a.id, outcome: "SUCCEEDED" })
                  }
                >
                  确认已完成
                </button>
                <button
                  disabled={reconcile.isPending}
                  onClick={() =>
                    reconcile.mutate({ id: a.id, outcome: "FAILED" })
                  }
                >
                  确认未完成
                </button>
              </div>
            </div>
          )}
        </details>
      ))}
      {actions.data?.length === 0 && <p className="muted">尚无动作记录</p>}
      {cancel && (
        <Modal title="停止任务？" onClose={() => setCancel(false)}>
          <p>Agent 将停止推进“{task.title}”。已经完成的动作和文件会保留。</p>
          <div className="button-row">
            <button
              className="danger"
              disabled={command.isPending}
              onClick={() =>
                command.mutate("cancel", { onSuccess: () => setCancel(false) })
              }
            >
              确认停止
            </button>
            <button onClick={() => setCancel(false)}>继续任务</button>
          </div>
        </Modal>
      )}
    </aside>
  );
}
