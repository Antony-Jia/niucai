import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { App } from "../src/App";
import { Provider } from "../src/lib/state";
import { request } from "../src/lib/api";
vi.mock("@tauri-apps/api/core", () => ({
  isTauri: () => false,
  invoke: vi.fn(),
}));
async function explore() {
  render(
    <Provider>
      <App />
    </Provider>,
  );
  fireEvent.click(await screen.findByRole("button", { name: "体验示例" }));
  await screen.findByText("示例模式 · 不连接真实模型或设备");
}
function nav(name: string) {
  fireEvent.click(
    screen.getByRole("button", { name: new RegExp(`^${name} `) }),
  );
}
describe("Desktop workflows", () => {
  it("requires native desktop to connect, and exposes a clearly marked demo", async () => {
    await expect(request("/api/tasks")).rejects.toThrow("Windows 桌面客户端");
    await explore();
    expect(await screen.findByText("正在执行")).toBeInTheDocument();
  });
  it("creates a task with a goal, pauses it and resumes from persisted state", async () => {
    await explore();
    nav("Tasks");
    fireEvent.click(screen.getByRole("button", { name: "新建任务" }));
    fireEvent.change(screen.getByLabelText("任务名称"), {
      target: { value: "核验 Windows 流程" },
    });
    fireEvent.change(screen.getByLabelText("目标"), {
      target: { value: "检查任务状态是否可以恢复" },
    });
    fireEvent.click(screen.getByRole("button", { name: "交给 Agent 执行" }));
    await screen.findByText("任务已创建");
    await waitFor(() =>
      expect(screen.getAllByText("核验 Windows 流程")).toHaveLength(2),
    );
    fireEvent.click(screen.getByRole("button", { name: "暂停" }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "恢复" })).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByRole("button", { name: "恢复" }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "暂停" })).toBeInTheDocument(),
    );
  });
  it("changes control ownership before showing desktop controls", async () => {
    await explore();
    nav("Computer");
    await screen.findByRole("button", { name: "接管电脑" });
    expect(
      screen.queryByRole("button", { name: "打开远程桌面" }),
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "接管电脑" }));
    expect(
      await screen.findByRole("button", { name: "打开远程桌面" }),
    ).toBeDisabled();
    expect(screen.getByText("控制权在你手中")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "交还 Agent" }));
    await screen.findByRole("button", { name: "接管电脑" });
  });
});

it("keeps chat separate from tasks until the user explicitly creates one", async () => {
  await explore();
  nav("Chat");
  fireEvent.change(screen.getByLabelText("聊天内容"), {
    target: { value: "帮我整理 Windows 接入清单" },
  });
  fireEvent.click(screen.getByRole("button", { name: "发送消息" }));
  await screen.findByText("帮我整理 Windows 接入清单", { selector: "p" });
  expect((await request<unknown[]>("/api/tasks")).length).toBe(3);
  fireEvent.click(screen.getByRole("button", { name: "交给 Agent 执行" }));
  await screen.findByText("任务已创建");
  expect((await request<unknown[]>("/api/tasks")).length).toBe(4);
});

it("removes a denied approval from the waiting queue", async () => {
  await explore();
  fireEvent.click(await screen.findByRole("button", { name: "拒绝" }));
  await waitFor(() =>
    expect(
      screen.queryByRole("button", { name: "拒绝" }),
    ).not.toBeInTheDocument(),
  );
  expect(await request("/api/approvals")).toEqual([]);
});
