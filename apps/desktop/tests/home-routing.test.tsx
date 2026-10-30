import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { App } from "../src/App";
import { Provider } from "../src/lib/state";
import * as apiModule from "../src/lib/api";

vi.mock("@tauri-apps/api/core", () => ({
  isTauri: () => false,
  invoke: vi.fn(),
}));

const override = vi.hoisted(() => ({
  handler: undefined as
    undefined | ((route: string, method: string, data: unknown) => unknown),
}));
vi.mock("../src/lib/api", async (original) => {
  const real = await original<typeof import("../src/lib/api")>();
  return {
    ...real,
    request: vi.fn(async (route: string, method = "GET", data?: unknown) => {
      const custom = override.handler?.(route, method, data);
      if (custom !== undefined) return custom;
      return real.request(route, method, data);
    }),
  };
});

async function explore() {
  render(
    <Provider>
      <App />
    </Provider>,
  );
  fireEvent.click(await screen.findByRole("button", { name: "体验示例" }));
  await screen.findByText("示例模式 · 不连接真实模型或设备");
}

describe("home routing", () => {
  it("retries Goal creation with the same idempotency key after a lost response", async () => {
    const posts: { client_id?: string; message: string }[] = [];
    override.handler = (route, method, data) => {
      if (route === "/api/kernel/diagnostics")
        return { features: { orchestrator: true, legacy_execution: false } };
      if (route === "/api/goals" && method === "POST") {
        posts.push(data as { client_id?: string; message: string });
        // 第一次：服务端已创建，但响应丢失。
        if (posts.length === 1) throw new Error("网络中断");
        return { id: "goal-created" };
      }
      return undefined;
    };
    try {
      await explore();
      const button = await screen.findByRole("button", { name: "开始任务" });
      fireEvent.change(screen.getByLabelText("任务目标"), {
        target: { value: "整理周报" },
      });
      await waitFor(() => expect(button).toBeEnabled());
      fireEvent.click(button);
      await waitFor(() => expect(posts).toHaveLength(1));
      await waitFor(() => expect(button).toBeEnabled());
      fireEvent.click(button);
      await waitFor(() => expect(posts).toHaveLength(2));
      expect(posts[0].client_id).toBeTruthy();
      expect(posts[1].client_id).toBe(posts[0].client_id);
    } finally {
      override.handler = undefined;
    }
  });

  it("keeps stable accessible names on the navigation, with the badge as a description", async () => {
    await explore();
    const nav = screen.getByRole("navigation", { name: "主导航" });
    for (const name of ["任务", "文件", "设备"])
      expect(within(nav).getByRole("button", { name })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "设置" })).toBeInTheDocument();
    const tasks = within(nav).getByRole("button", {
      name: "任务",
    });
    // 示例数据里有需要处理的任务：数量通过描述提供，不改变按钮名称。
    await waitFor(() =>
      expect(tasks).toHaveAccessibleDescription(/项任务需要你处理/),
    );
  });

  it("renders a single page heading on the devices page", async () => {
    await explore();
    fireEvent.click(screen.getByRole("button", { name: "设备" }));
    await screen.findByRole("heading", { level: 1, name: "设备" });
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
  });

  it("opens a standalone task in its own detail page, not the conversation workbench", async () => {
    await explore();
    await apiModule.request("/api/tasks", "POST", {
      title: "独立旧任务",
      goal: "没有会话",
    });
    fireEvent.click(screen.getByRole("button", { name: "刷新" }));
    fireEvent.click(await screen.findByRole("button", { name: /独立旧任务/ }));
    expect(
      await screen.findByRole("heading", { name: "任务详情" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "独立旧任务" })).toBeVisible();
    expect(screen.queryByLabelText("聊天内容")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "返回任务" }));
    await screen.findByRole("region", { name: "我的任务" });
  });
});
