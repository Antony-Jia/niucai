import { useMemo } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Check,
  CircleSlash,
  LoaderCircle,
  MessageSquare,
  Terminal,
  TriangleAlert,
  Wrench,
  X,
} from "lucide-react";
import { allJobEvents } from "../lib/board";
import {
  buildJobLog,
  clock,
  duration,
  engineLabels,
  toolLabel,
  type JobEvent,
  type LogEntry,
  type LogSummary,
} from "../lib/job-log";
import { ErrorNotice, Loading } from "./ui";

const finished = new Set(["COMPLETED", "FAILED", "CANCELLED", "DENIED"]);
const longText = 280;

const toolIcon = {
  running: <LoaderCircle className="spin" size={13} />,
  ok: <Check size={13} />,
  error: <X size={13} />,
  stopped: <CircleSlash size={13} />,
};
const toolStateText = {
  running: "进行中",
  ok: "成功",
  error: "失败",
  stopped: "未结束",
};

function Summary({ summary, engine }: { summary: LogSummary; engine: string }) {
  const name =
    engineLabels[summary.engine || engine] ?? (summary.engine || engine);
  const usage = summary.usage;
  const tokens = (usage.input_tokens ?? 0) + (usage.output_tokens ?? 0);
  return (
    <p className="job-log-summary">
      <span>{name}</span>
      <span>{summary.toolCalls} 次工具调用</span>
      {summary.toolErrors > 0 && (
        <span className="job-log-bad">{summary.toolErrors} 次失败</span>
      )}
      {summary.durationMs !== null && (
        <span>
          {summary.finished ? "用时 " : "已运行 "}
          {duration(summary.durationMs)}
        </span>
      )}
      {tokens > 0 && <span>{tokens.toLocaleString()} tokens</span>}
      {summary.costUsd !== null && <span>${summary.costUsd.toFixed(4)}</span>}
    </p>
  );
}

/** 命令或参数一行放得下就直接显示，否则折叠。 */
function ToolDetail({ entry }: { entry: Extract<LogEntry, { type: "tool" }> }) {
  const { input, output, exitCode } = entry;
  if (!input && !output && exitCode === undefined) return null;
  const oneLine = input && input.length <= 120 && !input.includes("\n");
  return (
    <div className="job-log-detail">
      {input &&
        (oneLine ? (
          <code className="job-log-input">{input}</code>
        ) : (
          <details>
            <summary>
              <code className="job-log-input">{input.slice(0, 120)}…</code>
            </summary>
            <pre>{input}</pre>
          </details>
        ))}
      {(output || (exitCode !== undefined && exitCode !== 0)) && (
        <details open={entry.state === "error"}>
          <summary>
            输出
            {exitCode !== undefined && (
              <span className={exitCode === 0 ? "" : "job-log-bad"}>
                {" "}
                · 退出码 {exitCode}
              </span>
            )}
          </summary>
          {output && <pre>{output}</pre>}
        </details>
      )}
    </div>
  );
}

function Entry({ entry }: { entry: LogEntry }) {
  if (entry.type === "tool")
    return (
      <li className={`job-log-entry tool ${entry.state}`}>
        <time>{clock(entry.at)}</time>
        <span className="job-log-icon">
          <Wrench size={13} />
        </span>
        <span className="job-log-body">
          <strong>{toolLabel(entry.tool)}</strong>
          {toolLabel(entry.tool) !== entry.tool && <code>{entry.tool}</code>}
          <ToolDetail entry={entry} />
        </span>
        <span className="job-log-state">
          {toolIcon[entry.state]} {toolStateText[entry.state]}
          {entry.durationMs !== undefined && entry.state !== "running"
            ? ` · ${duration(entry.durationMs)}`
            : ""}
        </span>
      </li>
    );
  if (entry.type === "stderr")
    return (
      <li className="job-log-entry stderr">
        <time>{clock(entry.at)}</time>
        <span className="job-log-icon">
          <Terminal size={13} />
        </span>
        <details className="job-log-body">
          <summary>引擎错误输出</summary>
          <pre>{entry.text}</pre>
        </details>
      </li>
    );
  if (entry.type === "assistant")
    return (
      <li className="job-log-entry assistant">
        <time>{clock(entry.at)}</time>
        <span className="job-log-icon">
          <MessageSquare size={13} />
        </span>
        {entry.text.length > longText ? (
          <details className="job-log-body">
            <summary>{entry.text.slice(0, longText)}…</summary>
            <p>{entry.text}</p>
          </details>
        ) : (
          <p className="job-log-body">{entry.text}</p>
        )}
      </li>
    );
  return (
    <li className={`job-log-entry notice ${entry.tone}`}>
      <time>{clock(entry.at)}</time>
      <span className="job-log-icon">
        <TriangleAlert size={13} />
      </span>
      <span className="job-log-body">{entry.text}</span>
    </li>
  );
}

/** 一次 Attempt 的格式化执行日志：Pi、Codex、Claude 共用同一组事件。 */
export function JobLog({
  jobId,
  engine,
  status,
}: {
  jobId: string;
  engine: string;
  status: string;
}) {
  const cache = useQueryClient();
  const live = !finished.has(status);
  const key = ["job-events", jobId];
  const events = useQuery({
    queryKey: key,
    // 只取游标之后的新事件，累加到已缓存的列表。
    queryFn: async () => {
      const known = cache.getQueryData<JobEvent[]>(key) ?? [];
      return [...known, ...(await allJobEvents(jobId, known.at(-1)?.id ?? 0))];
    },
    refetchInterval: live ? 2000 : false,
  });
  const log = useMemo(() => buildJobLog(events.data ?? []), [events.data]);
  if (events.isLoading) return <Loading />;
  return (
    <div className="job-log">
      <ErrorNotice error={events.error} />
      {events.data && events.data.length > 0 && (
        <Summary summary={log.summary} engine={engine} />
      )}
      <ol className="job-log-list">
        {log.entries.map((entry) => (
          <Entry key={entry.key} entry={entry} />
        ))}
      </ol>
      {events.data?.length === 0 && <p className="muted">暂无事件</p>}
      {log.summary.toolCalls > 0 &&
        !log.entries.some(
          (e) => e.type === "tool" && (e.input || e.output),
        ) && (
          <p className="muted job-log-note">
            这次执行没有记录命令参数和输出（旧版 Runner 或已关闭详情）。
          </p>
        )}
    </div>
  );
}
