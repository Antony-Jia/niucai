import { useQuery } from "@tanstack/react-query";
import { Header, Badge, ErrorNotice, Loading } from "../components/ui";
import { request } from "../lib/api";
import { useApp } from "../lib/state";

interface KanbanCard {
  id: string;
  title: string;
  status: string;
  column: string;
  updated_at: string;
  agents: { id: string; name: string; role: string }[];
  remote_jobs: {
    id: string;
    engine: string;
    status: string;
    node_name: string | null;
  }[];
}
interface KanbanResource {
  id: string;
  name: string;
  kind: "node" | "computer";
  resource_type?: string | null;
  status: string;
  busy: boolean;
  job?: {
    engine?: string;
    prompt?: string;
    task_title?: string | null;
    control?: string;
  } | null;
}
interface KanbanData {
  tasks: KanbanCard[];
  resources: KanbanResource[];
}

const columns: [string, string][] = [
  ["PENDING", "等待执行"],
  ["RUNNING", "执行中"],
  ["WAITING_HUMAN", "等待你确认"],
  ["WAITING_BOT", "等待协作"],
  ["PAUSED", "已暂停"],
  ["DONE", "近 24 小时已结束"],
];
const roles: Record<string, string> = {
  lead: "主责",
  assignee: "负责",
  delegate: "受委派",
  delegator: "委派方",
  owner: "主责",
  member: "协作",
};

export function Kanban() {
  const { connected } = useApp();
  const board = useQuery({
    queryKey: ["kanban"],
    queryFn: () => request<KanbanData>("/api/kanban"),
    enabled: connected,
    refetchInterval: 3000,
  });
  const data = board.data;
  return (
    <>
      <Header
        eyebrow="KANBAN"
        title="任务看板"
        description="查看进行中的任务、支撑它们的 Agent，以及 VPS 与电脑正在忙什么。"
      />
      <ErrorNotice error={board.error} />
      {board.isLoading && <Loading />}
      {data && (
        <>
          <section className="panel">
            <div className="section-heading">
              <h2>资源占用</h2>
            </div>
            {data.resources.length === 0 ? (
              <p className="quiet-note">暂无 VPS 节点或电脑</p>
            ) : (
              <div className="kanban-resources">
                {data.resources.map((r) => (
                  <div
                    key={`${r.kind}-${r.id}`}
                    className={
                      r.busy ? "kanban-resource busy" : "kanban-resource"
                    }
                  >
                    <strong>{r.name}</strong>
                    <small>
                      {r.kind === "node" ? "VPS / 资源节点" : "电脑"}
                      {r.resource_type && r.resource_type !== "unspecified"
                        ? ` · ${r.resource_type}`
                        : ""}
                    </small>
                    <span>
                      <Badge status={r.status} /> {r.busy ? "忙碌" : "空闲"}
                    </span>
                    {r.busy && r.job && (
                      <small>
                        {r.job.task_title || "未关联任务"}
                        {r.job.engine ? ` · ${r.job.engine}` : ""}
                        {r.job.prompt ? ` — ${r.job.prompt}` : ""}
                      </small>
                    )}
                  </div>
                ))}
              </div>
            )}
          </section>
          <div className="kanban-board">
            {columns.map(([key, label]) => {
              const cards = data.tasks.filter((t) => t.column === key);
              return (
                <section key={key} className="kanban-column">
                  <h3>
                    {label}
                    <span>{cards.length}</span>
                  </h3>
                  {cards.map((t) => (
                    <article key={t.id} className="kanban-card">
                      <strong>{t.title}</strong>
                      <Badge status={t.status} />
                      <div className="kanban-chips">
                        {t.agents.map((a) => (
                          <span key={a.id} className="kanban-chip">
                            {a.name} · {roles[a.role] || a.role}
                          </span>
                        ))}
                      </div>
                      {t.remote_jobs.map((j) => (
                        <small key={j.id}>
                          远端 {j.engine} · {j.node_name || "排队中"} ·{" "}
                          {j.status}
                        </small>
                      ))}
                    </article>
                  ))}
                </section>
              );
            })}
          </div>
        </>
      )}
    </>
  );
}
