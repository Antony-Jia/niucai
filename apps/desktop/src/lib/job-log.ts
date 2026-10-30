/** 把 Attempt 的 remote.job.* 事件整理成可读的执行日志；Pi / Codex / Claude 的事件已由 Runner 统一，这里不区分引擎。 */

export interface JobEvent {
  id: number;
  type: string;
  created_at: string;
  data: {
    kind?: string;
    sequence?: number;
    status?: string;
    reason?: string;
    result?: Record<string, unknown>;
    payload?: Record<string, unknown>;
  };
}

/** stopped：作业已结束但这次工具调用没有收到结束事件（被取消或中断）。 */
export type ToolState = "running" | "ok" | "error" | "stopped";
export type NoticeTone = "info" | "warn" | "error";

export type LogEntry =
  | {
      type: "tool";
      key: string;
      at: string;
      tool: string;
      state: ToolState;
      durationMs?: number;
      input?: string;
      output?: string;
      exitCode?: number;
    }
  | { type: "assistant"; key: string; at: string; text: string }
  | { type: "notice"; key: string; at: string; text: string; tone: NoticeTone }
  | { type: "stderr"; key: string; at: string; text: string };

export interface LogSummary {
  engine: string;
  sessionId: string;
  toolCalls: number;
  toolErrors: number;
  startedAt: string | null;
  endedAt: string | null;
  durationMs: number | null;
  finished: boolean;
  error: string;
  costUsd: number | null;
  usage: Record<string, number>;
}

export interface JobLog {
  entries: LogEntry[];
  summary: LogSummary;
}

/** Codex 把所有工具归为几个通用名称，其余引擎直接使用工具原名。 */
const toolLabels: Record<string, string> = {
  shell: "执行命令",
  edit: "修改文件",
  mcp: "MCP 调用",
  web_search: "网页搜索",
};
export const toolLabel = (tool: string) => toolLabels[tool] ?? tool;

export const engineLabels: Record<string, string> = {
  pi: "Pi",
  codex: "Codex",
  claude: "Claude Code",
};

const lifecycle: Record<string, { text: string; tone: NoticeTone }> = {
  started: { text: "开始执行", tone: "info" },
  recovered: { text: "连接恢复，继续执行", tone: "info" },
  lost: { text: "节点失联，等待恢复", tone: "warn" },
  cancel_requested: { text: "已请求取消", tone: "warn" },
  approve: { text: "已批准执行", tone: "info" },
  deny: { text: "已拒绝执行", tone: "warn" },
};

export function parseTime(value: string) {
  return new Date(value.endsWith("Z") ? value : value + "Z").getTime();
}

export function clock(value: string) {
  const date = new Date(parseTime(value));
  return isNaN(date.getTime())
    ? value
    : new Intl.DateTimeFormat("zh-CN", {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hour12: false,
      }).format(date);
}

export function duration(ms: number) {
  if (!(ms >= 0)) return "";
  if (ms < 1000) return `${Math.round(ms)} 毫秒`;
  const seconds = Math.round(ms / 1000);
  if (seconds < 60) return `${seconds} 秒`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes} 分 ${seconds % 60} 秒`;
}

const text = (value: unknown) => (typeof value === "string" ? value : "");
/** 输入与输出都是可选字段：旧 Runner 或关闭详情时不会带。 */
const optional = (value: unknown) => text(value) || undefined;
const numbers = (value: unknown) =>
  Object.fromEntries(
    Object.entries((value as Record<string, unknown>) ?? {}).filter(
      (pair): pair is [string, number] => typeof pair[1] === "number",
    ),
  );

export function buildJobLog(events: JobEvent[]): JobLog {
  const entries: LogEntry[] = [];
  // 工具事件不带调用 ID：同名工具按先进先出配对。
  const open: Record<string, number[]> = {};
  const summary: LogSummary = {
    engine: "",
    sessionId: "",
    toolCalls: 0,
    toolErrors: 0,
    startedAt: null,
    endedAt: null,
    durationMs: null,
    finished: false,
    error: "",
    costUsd: null,
    usage: {},
  };
  const push = (entry: LogEntry) => entries.push(entry);

  for (const event of events) {
    const name = event.type.replace("remote.job.", "");
    const key = String(event.id);
    summary.startedAt ??= event.created_at;
    if (name === "progress") {
      const payload = event.data.payload ?? {};
      summary.engine ||= text(payload.engine);
      switch (event.data.kind) {
        case "session":
          summary.sessionId = text(payload.session_id);
          break;
        case "assistant":
          if (text(payload.text).trim())
            push({
              type: "assistant",
              key,
              at: event.created_at,
              text: text(payload.text),
            });
          break;
        case "tool_execution_start": {
          const tool = text(payload.tool) || "tool";
          summary.toolCalls += 1;
          (open[tool] ??= []).push(entries.length);
          push({
            type: "tool",
            key,
            at: event.created_at,
            tool,
            state: "running",
            input: optional(payload.input),
          });
          break;
        }
        case "tool_execution_end": {
          const tool = text(payload.tool) || "tool";
          const state: ToolState = payload.is_error ? "error" : "ok";
          const exitCode =
            typeof payload.exit_code === "number"
              ? payload.exit_code
              : undefined;
          if (state === "error") summary.toolErrors += 1;
          const index = open[tool]?.shift();
          const started = index === undefined ? undefined : entries[index];
          if (started?.type === "tool") {
            started.state = state;
            started.durationMs =
              parseTime(event.created_at) - parseTime(started.at);
            started.input ||= optional(payload.input);
            started.output = optional(payload.output);
            started.exitCode = exitCode;
          } else {
            // 开始事件缺失（例如补传前被截断）：仍然记下这次调用。
            summary.toolCalls += 1;
            push({
              type: "tool",
              key,
              at: event.created_at,
              tool,
              state,
              input: optional(payload.input),
              output: optional(payload.output),
              exitCode,
            });
          }
          break;
        }
        case "stderr":
          if (text(payload.text).trim())
            push({
              type: "stderr",
              key,
              at: event.created_at,
              text: text(payload.text),
            });
          break;
        case "notice":
          push({
            type: "notice",
            key,
            at: event.created_at,
            text: text(payload.text),
            tone: "warn",
          });
          break;
      }
    } else if (name === "finished") {
      const result = event.data.result ?? {};
      summary.finished = true;
      summary.endedAt = event.created_at;
      summary.error = text(result.error);
      summary.costUsd =
        typeof result.cost_usd === "number" ? result.cost_usd : null;
      summary.usage = numbers(result.usage);
      if (result.unknown_frames)
        push({
          type: "notice",
          key: `${key}-drift`,
          at: event.created_at,
          text: `有 ${String(result.unknown_frames)} 条引擎输出未识别，可能是 CLI 版本升级导致格式变化`,
          tone: "warn",
        });
      push({
        type: "notice",
        key,
        at: event.created_at,
        text: summary.error
          ? `执行失败：${summary.error}`
          : event.data.status === "CANCELLED"
            ? "已取消"
            : "执行完成",
        tone:
          summary.error || event.data.status === "FAILED" ? "error" : "info",
      });
    } else if (lifecycle[name]) {
      push({
        type: "notice",
        key,
        at: event.created_at,
        text:
          lifecycle[name].text +
          (event.data.reason ? `（${event.data.reason}）` : ""),
        tone: lifecycle[name].tone,
      });
    }
  }
  if (summary.finished)
    for (const entry of entries)
      if (entry.type === "tool" && entry.state === "running")
        entry.state = "stopped";
  const last = events.at(-1);
  if (summary.startedAt && (summary.endedAt || last)) {
    summary.durationMs =
      parseTime(summary.endedAt ?? last!.created_at) -
      parseTime(summary.startedAt);
  }
  return { entries, summary };
}
