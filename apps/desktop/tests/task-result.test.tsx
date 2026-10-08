import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import * as transport from "../src/lib/api";
import { Provider, useApp } from "../src/lib/state";
import { TaskResult } from "../src/components/TaskResult";
import type { Task } from "../src/lib/types";

vi.mock("@tauri-apps/api/core", () => ({
  isTauri: () => false,
  invoke: vi.fn(),
}));
afterEach(() => vi.restoreAllMocks());
const task = {
  id: "one",
  status: "FAILED",
  checkpoint: { result: "已完成第一步" },
} as Task;
function Harness() {
  const { connected, connectTo } = useApp();
  return connected ? (
    <TaskResult
      task={task}
      actions={[
        {
          id: "ok",
          task_id: "one",
          spec: { type: "browser.navigate" },
          status: "SUCCEEDED",
          risk: "LOW",
          result: { url: "https://example.com" },
        },
        {
          id: "bad",
          task_id: "one",
          spec: { type: "browser.click" },
          status: "UNKNOWN",
          risk: "HIGH",
          result: {},
        },
      ]}
    />
  ) : (
    <button onClick={() => void connectTo({} as never)}>连接</button>
  );
}

it("loads task-scoped files and keeps downloads available when preview is unsupported", async () => {
  vi.spyOn(transport, "loadProfile").mockResolvedValue({
    has_token: false,
  } as never);
  vi.spyOn(transport, "connect").mockResolvedValue({
    has_token: true,
  } as never);
  vi.spyOn(transport.api, "events").mockResolvedValue([]);
  const artifacts = vi.spyOn(transport.api, "taskArtifacts").mockResolvedValue([
    {
      id: "shot",
      task_id: "one",
      path: "artifacts/one.png",
      media_type: "image/png",
      created_at: "",
    },
  ]);
  vi.spyOn(transport.api, "artifactPreview").mockRejectedValue(
    new Error("HTTP 404"),
  );
  const download = vi
    .spyOn(transport, "downloadArtifact")
    .mockResolvedValue(null);
  render(
    <Provider>
      <Harness />
    </Provider>,
  );
  fireEvent.click(screen.getByRole("button", { name: "连接" }));
  await screen.findByText("artifacts/one.png");
  expect(artifacts).toHaveBeenCalledWith("one");
  expect(screen.getByText("已成功动作：1 / 2")).toBeInTheDocument();
  expect(screen.getByText(/需核对的动作/)).toBeInTheDocument();
  expect(screen.getByText(/最后成功观测页面/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "预览截图" }));
  await screen.findByText(/截图预览不可用/);
  fireEvent.click(screen.getByRole("button", { name: "下载", exact: true }));
  await vi.waitFor(() => expect(download).toHaveBeenCalledWith("shot"));
  fireEvent.click(screen.getByRole("button", { name: "关闭预览" }));
  expect(screen.queryByText(/截图预览不可用/)).not.toBeInTheDocument();
});
