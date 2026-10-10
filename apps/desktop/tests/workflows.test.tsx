import {
  render,
  screen,
  fireEvent,
  waitFor,
  within,
} from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { App } from "../src/App";
import { Provider } from "../src/lib/state";
import { api, request } from "../src/lib/api";
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
  it("starts execution from a message and pauses/resumes the same round", async () => {
    await explore();
    nav("会话");
    fireEvent.change(screen.getByLabelText("聊天内容"), {
      target: { value: "核验 Windows 流程" },
    });
    fireEvent.click(screen.getByRole("button", { name: "发送消息" }));
    await screen.findByText("核验 Windows 流程", { selector: ".message p" });
    const session = screen.getByRole("region", { name: "统一会话" });
    fireEvent.click(within(session).getByRole("button", { name: "暂停" }));
    fireEvent.click(
      await within(session).findByRole("button", { name: "恢复" }),
    );
    await within(session).findByRole("button", { name: "暂停" });
    expect((await request<unknown[]>("/api/tasks")).length).toBe(4);
  });
  it("changes control ownership before showing desktop controls", async () => {
    await explore();
    nav("会话");
    await screen.findByRole("button", { name: "接管电脑" });
    expect(
      screen.queryByRole("button", { name: "打开远程桌面" }),
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "接管电脑" }));
    expect(
      await screen.findByRole("button", { name: "打开远程桌面" }),
    ).toBeDisabled();
    expect(screen.getByText("控制权在你手中")).toBeInTheDocument();
    fireEvent.click(
      within(screen.getByRole("region", { name: "云端电脑面板" })).getByRole(
        "button",
        { name: "交还 Agent" },
      ),
    );
    await screen.findByRole("button", { name: "接管电脑" });
  });
});

it("adds steering to the same conversation without another execution", async () => {
  await explore();
  nav("会话");
  fireEvent.change(screen.getByLabelText("聊天内容"), {
    target: { value: "帮我整理 Windows 接入清单" },
  });
  fireEvent.click(screen.getByRole("button", { name: "发送消息" }));
  await screen.findByText("帮我整理 Windows 接入清单", {
    selector: ".message p",
  });
  fireEvent.change(screen.getByLabelText("聊天内容"), {
    target: { value: "只研究，不写文件" },
  });
  fireEvent.click(screen.getByRole("button", { name: "发送消息" }));
  await screen.findByText("只研究，不写文件", { selector: ".message p" });
  expect((await request<unknown[]>("/api/tasks")).length).toBe(4);
  expect(
    screen.queryByRole("button", { name: "交给 Agent 执行" }),
  ).not.toBeInTheDocument();
  const created = await request<{ computer_id: string | null }[]>("/api/tasks");
  expect(created[0].computer_id).toBe("demo-computer");
});

it("keeps a new draft typed while the previous message is being acknowledged", async () => {
  await explore();
  nav("会话");
  let release!: () => void;
  const delayed = new Promise<void>((resolve) => {
    release = resolve;
  });
  const original = api.sendMessage;
  const admission = vi
    .spyOn(api, "sendMessage")
    .mockImplementation(async (...args) => {
      const result = await original(...args);
      await delayed;
      return result;
    });
  try {
    fireEvent.change(screen.getByLabelText("聊天内容"), {
      target: { value: "先研究需求" },
    });
    fireEvent.click(screen.getByRole("button", { name: "发送消息" }));
    await waitFor(() => expect(admission).toHaveBeenCalledOnce());
    fireEvent.change(screen.getByLabelText("聊天内容"), {
      target: { value: "补充要求：保留历史文件" },
    });
    release();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "发送消息" })).toBeEnabled(),
    );
    expect(screen.getByLabelText("聊天内容")).toHaveValue(
      "补充要求：保留历史文件",
    );
  } finally {
    release();
    admission.mockRestore();
  }
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

it("approves the selected task directly in Tasks", async () => {
  await explore();
  nav("会话");
  fireEvent.click(await screen.findByRole("button", { name: /保存研究结论/ }));
  fireEvent.click(await screen.findByRole("button", { name: "批准执行" }));
  await waitFor(() =>
    expect(
      screen.queryByRole("button", { name: "批准执行" }),
    ).not.toBeInTheDocument(),
  );
  expect(await request("/api/approvals")).toEqual([]);
});

it("does not show another task's approval in the selected detail", async () => {
  await explore();
  nav("会话");
  fireEvent.click(
    await screen.findByRole("button", {
      name: /调研 Agent Harness/,
    }),
  );
  expect(
    screen.queryByRole("button", { name: "批准执行" }),
  ).not.toBeInTheDocument();
});
