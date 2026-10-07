import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Provider, useApp } from "../src/lib/state";
import { api } from "../src/lib/api";
import { taskProgress } from "../src/lib/task-progress";
import { TaskDetail } from "../src/pages/Tasks";
import type { Task, TaskProgress } from "../src/lib/types";

vi.mock("@tauri-apps/api/core", () => ({
  isTauri: () => false,
  invoke: vi.fn(),
}));
afterEach(() => vi.restoreAllMocks());
const progress: TaskProgress = {
  phase: "MODEL_REQUEST",
  message: "正在等待模型响应",
  wait_reason: null,
  last_progress_at: "2026-10-07T04:00:00Z",
  allowed_operations: ["pause", "cancel"],
  action_id: null,
};
function task(patch: Partial<Task> = {}): Task {
  return {
    id: "demo-report",
    title: "进度测试",
    goal: "test",
    status: "RUNNING",
    computer_id: "demo-computer",
    created_at: progress.last_progress_at,
    updated_at: progress.last_progress_at,
    current_step: 0,
    retry_count: 0,
    plan: {},
    checkpoint: {},
    progress,
    ...patch,
  };
}
function Harness({ value, stale = false }: { value: Task; stale?: boolean }) {
  const { connected, explore } = useApp();
  return connected ? (
    <TaskDetail task={value} stale={stale} />
  ) : (
    <button onClick={explore}>打开测试</button>
  );
}
async function show(value: Task, stale = false) {
  render(
    <Provider>
      <Harness value={value} stale={stale} />
    </Provider>,
  );
  fireEvent.click(screen.getByRole("button", { name: "打开测试" }));
  await screen.findByRole("region", { name: "任务执行进度" });
}

describe("task progress and controls", () => {
  it("shows authoritative stage and business time despite stale checkpoint or zero actions", async () => {
    await show(task({ checkpoint: { reason: "WAITING_APPROVAL" } }));
    const section = screen.getByRole("region", { name: "任务执行进度" });
    expect(within(section).getByText("等待模型")).toBeInTheDocument();
    expect(within(section).getByText(/最后业务进展/)).toBeInTheDocument();
    expect(screen.queryByText("WAITING_APPROVAL")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "恢复" }),
    ).not.toBeInTheDocument();
  });
  it("offers approval instead of ordinary resume", async () => {
    await show(
      task({
        status: "WAITING_HUMAN",
        progress: {
          ...progress,
          phase: "WAITING_APPROVAL",
          message: "等待批准",
          wait_reason: "APPROVAL_REQUIRED",
          allowed_operations: ["pause", "cancel", "approve", "deny"],
        },
      }),
    );
    expect(
      await screen.findByRole("button", { name: "批准执行" }),
    ).toBeEnabled();
    expect(
      screen.queryByRole("button", { name: "恢复" }),
    ).not.toBeInTheDocument();
  });
  it("opens uncertain action details and requires separate reconciliation", async () => {
    vi.spyOn(api, "actions").mockResolvedValue([
      {
        id: "unknown",
        task_id: "demo-report",
        spec: { type: "browser.click" },
        status: "UNKNOWN",
        risk: "HIGH",
        result: {},
      },
    ]);
    await show(
      task({
        status: "WAITING_HUMAN",
        progress: {
          ...progress,
          phase: "WAITING_RECONCILIATION",
          message: "请核对实际结果",
          wait_reason: "ACTION_UNKNOWN",
          allowed_operations: ["pause", "cancel", "reconcile"],
          action_id: "unknown",
        },
      }),
    );
    expect(
      await screen.findByRole("button", { name: "确认已完成" }),
    ).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "恢复" }),
    ).not.toBeInTheDocument();
  });
  it("disables stale controls without changing server task state", async () => {
    await show(task(), true);
    expect(screen.getByText(/服务器任务状态待同步/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "暂停" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "停止任务" })).toBeDisabled();
  });
  it("submits only once while pending, reports failure inline and allows another attempt", async () => {
    let reject!: (reason: Error) => void;
    const control = vi
      .spyOn(api, "taskControl")
      .mockImplementationOnce(
        () =>
          new Promise((_, r) => {
            reject = r;
          }),
      )
      .mockRejectedValueOnce(new Error("仍然失败"));
    await show(task());
    const pause = screen.getByRole("button", { name: "暂停" });
    fireEvent.click(pause);
    fireEvent.click(pause);
    await waitFor(() => expect(control).toHaveBeenCalledTimes(1));
    expect(pause).toBeDisabled();
    expect(screen.getByText(/等待 Kernel 确认/)).toBeInTheDocument();
    reject(new Error("连接失败，请重试"));
    await screen.findByText("连接失败，请重试");
    await waitFor(() => expect(pause).toBeEnabled());
    fireEvent.click(pause);
    await waitFor(() => expect(control).toHaveBeenCalledTimes(2));
  });
  it("keeps legacy fallback honest and hides terminal wait prompts", () => {
    const legacy = task({
      progress: undefined,
      checkpoint: { reason: "UNKNOWN" },
      status: "WAITING_HUMAN",
    });
    expect(taskProgress(legacy).last_progress_at).toBe("");
    expect(taskProgress(legacy).allowed_operations).not.toContain("resume");
    const completed = taskProgress({ ...legacy, status: "COMPLETED" });
    expect(completed.wait_reason).toBeNull();
    expect(completed.allowed_operations).toEqual([]);
  });
});
