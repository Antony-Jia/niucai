import { ActionCard } from "../components/ActionCard";
import { describeAction } from "../lib/describe";
import { ShieldCheck } from "lucide-react";
import { api } from "../lib/api";
import { useApp, useCommand, useData } from "../lib/state";
import { ErrorNotice } from "../components/ui";
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
              {describeAction(a.action.spec).title}
            </span>
            <span className="risk">需要授权</span>
          </div>
          <p>任务 {a.action.task_id.slice(0, 8)} 提出了以下动作：</p>
          <ActionCard spec={a.action.spec} showTitle={false} />
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
