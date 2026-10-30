import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../src/App";
import { Provider } from "../src/lib/state";
import {
  activeMention,
  insertMention,
  mentionedBots,
} from "../src/lib/mentions";
import { isRemovable } from "../src/lib/task-view";

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

describe("@ mentions", () => {
  it("finds the mention being typed before the caret", () => {
    expect(activeMention("请 @res", 6)).toEqual({ start: 2, query: "res" });
    expect(activeMention("@", 1)).toEqual({ start: 0, query: "" });
    expect(activeMention("邮箱 a@b", 6)).toBeNull();
    expect(activeMention("hello @r world", 14)).toBeNull();
  });

  it("replaces the fragment with the chosen bot and keeps the tail", () => {
    const text = "请 @re 整理周报";
    const mention = activeMention(text, 5)!;
    const next = insertMention(text, 5, mention, "researcher");
    expect(next.text).toBe("请 @researcher 整理周报");
    expect(next.caret).toBe("请 @researcher ".length);
  });

  it("only counts known bots, once each, in order", () => {
    const names = ["researcher", "writer"];
    expect(
      mentionedBots("@writer 和 @researcher，再 @writer，别的 @nobody", names),
    ).toEqual(["writer", "researcher"]);
    expect(mentionedBots("mail me@researcher.com", names)).toEqual([]);
  });
});

describe("removing finished tasks", () => {
  it("only offers removal for finished or idle work", () => {
    expect(isRemovable({ status: "completed" })).toBe(true);
    expect(isRemovable({ status: "failed" })).toBe(true);
    expect(isRemovable({ status: "idle" })).toBe(true);
    expect(isRemovable({ status: "running" })).toBe(false);
    expect(isRemovable({ status: "attention" })).toBe(false);
  });
});

describe("home actions", () => {
  beforeEach(() => {
    // 视图偏好会跨用例保留：每个用例都从列表开始。
    localStorage.clear();
  });

  it("shows every bot's busy state and passes @-chosen bots to the goal", async () => {
    const posts: { message: string; bots?: string[] }[] = [];
    override.handler = (route, method, data) => {
      if (route === "/api/kernel/diagnostics")
        return { features: { orchestrator: true, legacy_execution: false } };
      if (route === "/api/goals" && method === "POST") {
        posts.push(data as { message: string; bots?: string[] });
        return { id: "goal-created" };
      }
      return undefined;
    };
    try {
      await explore();
      const roster = await screen.findByRole("region", { name: "助手状态" });
      const chip = await within(roster).findByRole("button", {
        name: /@researcher，/,
      });
      expect(chip).toHaveAccessibleName(/工作中|待命|不可用|状态未知/);
      fireEvent.click(chip);
      const box = screen.getByLabelText("任务目标") as HTMLTextAreaElement;
      expect(box.value).toBe("@researcher ");
      fireEvent.change(box, {
        target: { value: "@researcher 整理周报", selectionStart: 20 },
      });
      await waitFor(() =>
        expect(screen.getByRole("status")).toHaveTextContent(/@researcher/),
      );
      const start = screen.getByRole("button", { name: "开始任务" });
      await waitFor(() => expect(start).toBeEnabled());
      fireEvent.click(start);
      await waitFor(() => expect(posts).toHaveLength(1));
      expect(posts[0].bots).toEqual(["researcher"]);
    } finally {
      override.handler = undefined;
    }
  });

  it("offers the work map right under the roster", async () => {
    await explore();
    const roster = await screen.findByRole("region", { name: "助手状态" });
    fireEvent.click(
      within(roster).getByRole("button", { name: "打开工作地图" }),
    );
    expect(
      await within(roster).findByRole("button", { name: "正在查看工作地图" }),
    ).toBeInTheDocument();
  });

  it("deletes a finished task after confirming, and never offers it for running ones", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    await explore();
    await screen.findByText("梳理项目架构", {}, { timeout: 5000 });
    expect(
      screen.queryByRole("button", { name: /删除任务 调研 Agent Harness/ }),
    ).not.toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", { name: "删除任务 梳理项目架构" }),
    );
    await waitFor(() =>
      expect(screen.queryByText("梳理项目架构")).not.toBeInTheDocument(),
    );
    expect(screen.getByText("调研 Agent Harness 的最新进展")).toBeVisible();
  });

  it("keeps the task when the deletion is not confirmed", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(false);
    await explore();
    await screen.findByText("梳理项目架构", {}, { timeout: 5000 });
    fireEvent.click(
      screen.getByRole("button", { name: "删除任务 梳理项目架构" }),
    );
    expect(screen.getByText("梳理项目架构")).toBeVisible();
  });
});
