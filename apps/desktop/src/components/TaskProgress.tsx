import { phaseLabels } from "../lib/task-progress";
import type { TaskProgress as Progress } from "../lib/types";
import { time } from "./ui";

export function TaskProgress({
  progress,
  legacy,
  stale,
}: {
  progress: Progress;
  legacy: boolean;
  stale: boolean;
}) {
  return (
    <section
      className="task-progress"
      aria-label="任务执行进度"
      aria-live="polite"
    >
      <strong>{phaseLabels[progress.phase] || "任务状态"}</strong>
      <p>{progress.message}</p>
      {progress.last_progress_at && (
        <small>最后业务进展：{time(progress.last_progress_at)}</small>
      )}
      {legacy && <small>旧版 Kernel：详细阶段与进展时间暂不可用。</small>}
      {stale && (
        <p className="progress-offline" role="status">
          连接中断，服务器任务状态待同步；以下为最后已知状态。
        </p>
      )}
    </section>
  );
}
