import { expect, it } from "vitest";
import { buildJobLog, duration, type JobEvent } from "../src/lib/job-log";

let id = 0;
function event(
  name: string,
  second: number,
  data: JobEvent["data"] = {},
): JobEvent {
  id += 1;
  return {
    id,
    type: `remote.job.${name}`,
    created_at: `2026-10-30T08:00:${String(second).padStart(2, "0")}`,
    data,
  };
}
const progress = (
  second: number,
  kind: string,
  payload: Record<string, unknown>,
) => event("progress", second, { kind, payload });

it("pairs tool start/end events and summarises the attempt", () => {
  const log = buildJobLog([
    event("started", 0),
    progress(1, "session", { session_id: "s1", engine: "codex" }),
    progress(2, "tool_execution_start", { tool: "shell", engine: "codex" }),
    progress(5, "tool_execution_end", {
      tool: "shell",
      is_error: false,
      engine: "codex",
    }),
    progress(6, "tool_execution_start", { tool: "edit", engine: "codex" }),
    progress(7, "tool_execution_end", {
      tool: "edit",
      is_error: true,
      engine: "codex",
    }),
    progress(8, "assistant", { text: "done", engine: "codex" }),
    event("finished", 9, {
      status: "COMPLETED",
      result: { usage: { input_tokens: 10, output_tokens: 5 }, cost_usd: 0.5 },
    }),
  ]);
  const tools = log.entries.filter((e) => e.type === "tool");
  expect(tools).toHaveLength(2);
  expect(tools[0]).toMatchObject({
    tool: "shell",
    state: "ok",
    durationMs: 3000,
  });
  expect(tools[1]).toMatchObject({ tool: "edit", state: "error" });
  expect(log.summary).toMatchObject({
    engine: "codex",
    sessionId: "s1",
    toolCalls: 2,
    toolErrors: 1,
    finished: true,
    durationMs: 9000,
    costUsd: 0.5,
    usage: { input_tokens: 10, output_tokens: 5 },
  });
  expect(log.entries.at(-1)).toMatchObject({
    type: "notice",
    text: "执行完成",
  });
});

it("keeps a running tool open and marks it stopped once the job ends", () => {
  const running = buildJobLog([
    progress(1, "tool_execution_start", { tool: "Bash", engine: "claude" }),
  ]);
  expect(running.entries[0]).toMatchObject({ state: "running" });
  expect(running.summary.finished).toBe(false);

  const cancelled = buildJobLog([
    progress(1, "tool_execution_start", { tool: "Bash", engine: "claude" }),
    event("finished", 4, { status: "CANCELLED", result: {} }),
  ]);
  expect(cancelled.entries[0]).toMatchObject({ state: "stopped" });
  expect(cancelled.entries.at(-1)).toMatchObject({ text: "已取消" });
});

it("pairs same-named tools first in, first out", () => {
  const log = buildJobLog([
    progress(1, "tool_execution_start", { tool: "Read" }),
    progress(2, "tool_execution_start", { tool: "Read" }),
    progress(3, "tool_execution_end", { tool: "Read", is_error: true }),
    progress(4, "tool_execution_end", { tool: "Read", is_error: false }),
  ]);
  expect(log.entries.map((e) => e.type === "tool" && e.state)).toEqual([
    "error",
    "ok",
  ]);
});

it("reports failures, notices and format drift", () => {
  const log = buildJobLog([
    progress(1, "notice", { text: "MCP 未加载：缺少环境变量" }),
    event("lost", 2),
    event("finished", 3, {
      status: "FAILED",
      result: {
        error: "Pi ended without a successful final answer",
        unknown_frames: 2,
      },
    }),
  ]);
  const notices = log.entries.filter((e) => e.type === "notice");
  expect(notices.map((n) => n.type === "notice" && n.tone)).toEqual([
    "warn",
    "warn",
    "warn",
    "error",
  ]);
  expect(log.summary.error).toContain("Pi ended");
  expect(
    notices.some((n) => n.type === "notice" && n.text.includes("2 条")),
  ).toBe(true);
});

it("formats durations", () => {
  expect(duration(400)).toBe("400 毫秒");
  expect(duration(45000)).toBe("45 秒");
  expect(duration(125000)).toBe("2 分 5 秒");
});

it("carries tool input, output, exit code and stderr into the log", () => {
  const log = buildJobLog([
    progress(1, "tool_execution_start", {
      tool: "shell",
      input: "npm test",
      engine: "codex",
    }),
    progress(3, "tool_execution_end", {
      tool: "shell",
      is_error: true,
      output: "1 failed",
      exit_code: 1,
      engine: "codex",
    }),
    progress(4, "stderr", { text: "warn: deprecated", engine: "codex" }),
    progress(5, "stderr", { text: "  ", engine: "codex" }),
    progress(6, "tool_execution_end", {
      tool: "Read",
      is_error: false,
      input: "a.py",
      output: "x",
      engine: "claude",
    }),
  ]);
  expect(log.entries[0]).toMatchObject({
    input: "npm test",
    output: "1 failed",
    exitCode: 1,
    state: "error",
  });
  expect(log.entries[1]).toMatchObject({
    type: "stderr",
    text: "warn: deprecated",
  });
  // 没有开始事件时，结束事件自带的输入输出也要保留。
  expect(log.entries[2]).toMatchObject({ input: "a.py", output: "x" });
  expect(log.entries).toHaveLength(3);
});
