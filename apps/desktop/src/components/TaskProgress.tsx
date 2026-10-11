import { phaseLabels } from "../lib/task-progress";
import type { TaskProgress as Progress } from "../lib/types";
import { time } from "./ui";
import { useEffect, useState } from "react";

export function TaskProgress({
  progress,
  legacy,
  stale,
}: {
  progress: Progress;
  legacy: boolean;
  stale: boolean;
}) {
  const [now, setNow] = useState(Date.now);
  const active = [
    "QUEUED",
    "PLANNING",
    "MODEL_REQUEST",
    "PROCESSING",
    "WAITING_REMOTE",
    "RECOVERING",
    "EXECUTING_ACTION",
  ].includes(progress.phase);
  useEffect(() => {
    if (!active) return;
    const timer = setInterval(() => setNow(Date.now()), 15000);
    return () => clearInterval(timer);
  }, [active]);
  const seconds = Math.max(
    0,
    Math.floor((now - Date.parse(progress.last_progress_at)) / 1000),
  );
  const prolonged = active && !legacy && !stale && seconds >= 120;
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
      {prolonged && (
        <p className="progress-offline" role="status">
          已约 {Math.floor(seconds / 60)} 分钟没有新的业务进展。
          {progress.phase === "QUEUED"
            ? "任务尚未开始，请检查 Worker 与执行电脑；也可以暂停或停止排队。"
            : "可以刷新状态，或暂停、停止本轮执行；任务是否失败以 Kernel 状态为准。"}
        </p>
      )}
      {stale && (
        <p className="progress-offline" role="status">
          连接中断，服务器任务状态待同步；以下为最后已知状态。
        </p>
      )}
    </section>
  );
}
