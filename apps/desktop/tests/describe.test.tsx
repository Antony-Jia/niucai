import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import {
  describeAction,
  describeResult,
  eventLabel,
  plain,
} from "../src/lib/describe";
import { ActionCard } from "../src/components/ActionCard";
import { parsePairs, formatPairs } from "../src/pages/Extensions";
import { App } from "../src/App";
import { Provider } from "../src/lib/state";
import { api } from "../src/lib/api";

vi.mock("@tauri-apps/api/core", () => ({
  isTauri: () => false,
  invoke: vi.fn(),
}));

describe("readable actions", () => {
  it("describes every action kind in Chinese", () => {
    expect(
      describeAction({ type: "browser.navigate", url: "https://a.cn" }),
    ).toMatchObject({ title: "打开网页", summary: "https://a.cn" });
    expect(
      describeAction({ type: "shell.exec", argv: ["ls", "-la"] }).summary,
    ).toBe("ls -la");
    expect(describeAction({ type: "skills.read", name: "crawl" }).title).toBe(
      "查阅技能",
    );
    const mcp = describeAction({
      type: "mcp.call",
      server: "github",
      tool: "search",
      arguments: { query: "niucai", filters: { lang: "py" } },
    });
    expect(mcp.summary).toBe("github / search");
    expect(mcp.fields.map((f) => f.label)).toContain("参数 query");
    expect(mcp.fields.find((f) => f.label === "参数 filters")?.value).toBe(
      "lang：py",
    );
  });

  it("hides model-only fields and labels fake results", () => {
    const fields = describeResult({
      url: "https://a.cn",
      refs: ["e1"],
      elements: [{}, {}],
      truncated: true,
      adapter: "fake",
    });
    const labels = fields.map((f) => f.label);
    expect(labels).toEqual(["网址", "可操作元素", "提示", "适配器"]);
    expect(fields.at(-1)?.value).toContain("fake");
  });

  it("renders cards without raw JSON", () => {
    const { container } = render(
      <ActionCard
        spec={{ type: "files.write", path: "a.md", content: "hello" }}
        result={{ path: "a.md", bytes: 5 }}
        risk="HIGH"
      />,
    );
    expect(screen.getByText("写入文件")).toBeInTheDocument();
    expect(screen.getByText("高风险")).toBeInTheDocument();
    expect(container.textContent).not.toMatch(/[{}"]/);
  });

  it("labels collaboration and YOLO events", () => {
    expect(eventLabel("approval.auto_approved")).toBe("YOLO 自动批准");
    expect(
      eventLabel("workflow.pipeline_created", { stages: ["crawl", "report"] }),
    ).toBe("创建依赖流程 · crawl → report");
    expect(
      eventLabel("task.resumed", { reason: "pipeline dependencies settled" }),
    ).toBe("已恢复 · 上游阶段已完成");
    expect(eventLabel("conversation.message_created")).toBeNull();
    expect(plain({ a: [1, 2], b: true })).toBe("a：1、2\nb：是");
  });

  it("round-trips masked MCP secrets", () => {
    const env = parsePairs("TOKEN=***\nEMPTY\nURL=http://x?a=b", "=");
    expect(env).toEqual({ TOKEN: "***", URL: "http://x?a=b" });
    expect(formatPairs({ Authorization: "***" }, ":")).toBe(
      "Authorization: ***",
    );
  });
});

describe("conversation settings", () => {
  it("applies YOLO and chat mode to a new conversation before sending", async () => {
    render(
      <Provider>
        <App />
      </Provider>,
    );
    fireEvent.click(await screen.findByRole("button", { name: "体验示例" }));
    await screen.findByText("示例模式 · 不连接真实模型或设备");
    fireEvent.click(screen.getByRole("button", { name: /^会话 / }));
    fireEvent.click(await screen.findByRole("button", { name: /新建会话/ }));
    const yolo = await screen.findByLabelText("YOLO 电脑操作免审批");
    await waitFor(() => expect(yolo).toBeEnabled());
    fireEvent.click(yolo);
    fireEvent.change(screen.getByLabelText("聊天内容"), {
      target: { value: "抓取商品价格" },
    });
    fireEvent.click(screen.getByRole("button", { name: "发送消息" }));
    await screen.findByText("抓取商品价格", { selector: ".message p" });
    const created = (await api.conversations())[0];
    expect((await api.conversation(created.id)).settings).toEqual({
      mode: "agent",
      autonomy: { actions: true, remote: false },
    });
    fireEvent.change(await screen.findByLabelText("会话模式"), {
      target: { value: "chat" },
    });
    await waitFor(async () =>
      expect((await api.conversation(created.id)).settings?.mode).toBe("chat"),
    );
    expect(screen.queryByLabelText("YOLO 电脑操作免审批")).toBeNull();
  });
});
