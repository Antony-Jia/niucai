import type {
  Task,
  Computer,
  Memory,
  Artifact,
  Conversation,
  Message,
  Approval,
  Action,
  KernelEvent,
} from "./types";
const stamp = new Date().toISOString();
let tasks: Task[],
  computers: Computer[],
  memories: Memory[],
  conversations: Conversation[],
  messages: Message[],
  approvals: Approval[];
const task = (
  id: string,
  title: string,
  status: Task["status"],
  goal: string,
): Task => ({
  id,
  title,
  goal,
  status,
  computer_id: "demo-computer",
  created_at: stamp,
  updated_at: stamp,
  current_step: status === "RUNNING" ? 3 : 0,
  retry_count: 0,
  plan: {
    steps: [
      { id: "1", description: "收集公开资料", status: "completed" },
      { id: "2", description: "整理对比与来源", status: "pending" },
    ],
  },
  checkpoint:
    status === "COMPLETED"
      ? { result: "已整理结果，文件保存在 workspace。" }
      : {},
});
export function resetDemo() {
  tasks = [
    task(
      "demo-research",
      "调研 Agent Harness 的最新进展",
      "RUNNING",
      "比较三个开源 Agent Harness 的任务恢复、上下文管理和工具执行设计。",
    ),
    task(
      "demo-report",
      "保存研究结论",
      "WAITING_HUMAN",
      "将研究结论保存到 workspace。",
    ),
    task(
      "demo-completed",
      "梳理项目架构",
      "COMPLETED",
      "梳理 Kernel 的职责边界。",
    ),
  ];
  computers = [
    {
      id: "demo-computer",
      name: "Cloud Computer",
      kind: "linux",
      status: "ONLINE",
      control: "AGENT",
      state: { url: "https://example.com", title: "Research workspace" },
    },
  ];
  memories = [
    {
      id: "demo-memory",
      kind: "semantic",
      content: "用户偏好有代码、可复现和明确工程验证的技术资料。",
      tags: ["研究", "偏好"],
      created_at: stamp,
      task_id: null,
    },
  ];
  conversations = [];
  messages = [];
  approvals = [
    {
      id: "demo-approval",
      status: "PENDING",
      action: {
        id: "demo-action",
        task_id: "demo-report",
        spec: {
          type: "files.write",
          path: "research/harness-notes.md",
          content: "研究结论与来源列表",
        },
        status: "WAITING_APPROVAL",
        risk: "HIGH",
        result: {},
      },
    },
  ];
}
resetDemo();
export async function demoRequest(
  path: string,
  method: string,
  body: unknown,
): Promise<unknown> {
  const data = body as Record<string, string> | null;
  const route = path.split("?")[0];
  if (route === "/api/tasks" && method === "GET") return structuredClone(tasks);
  if (route === "/api/tasks" && data) {
    const t = task(crypto.randomUUID(), data.title, "PENDING", data.goal);
    t.computer_id = data.computer_id || null;
    tasks.unshift(t);
    return t;
  }
  if (route.match(/^\/api\/tasks\/[^/]+\/actions$/)) return [] as Action[];
  if (route.startsWith("/api/tasks/")) {
    const [, , , id, operation] = route.split("/");
    const t = tasks.find((t) => t.id === id);
    if (!t) throw new Error("任务不存在");
    if (method === "POST")
      t.status = (
        {
          pause: "PAUSED",
          resume: "PENDING",
          retry: "PENDING",
          cancel: "CANCELLED",
        } as Record<string, Task["status"]>
      )[operation];
    return structuredClone(t);
  }
  if (route === "/api/computers" && method === "GET")
    return structuredClone(computers);
  if (route === "/api/computers" && data) {
    const c: Computer = {
      id: crypto.randomUUID(),
      name: data.name,
      kind: "linux",
      status: "OFFLINE",
      control: "AGENT",
      state: {},
    };
    computers.push(c);
    return c;
  }
  if (route.startsWith("/api/computers/")) {
    const parts = route.split("/");
    const c = computers.find((c) => c.id === parts[3]);
    if (!c) throw new Error("设备不存在");
    c.control = (
      {
        "take-control": "HUMAN",
        "hand-back": "AGENT",
        pause: "PAUSED",
        resume: "AGENT",
      } as Record<string, Computer["control"]>
    )[parts[4]];
    return structuredClone(c);
  }
  if (route === "/api/approvals") return structuredClone(approvals);
  if (route.startsWith("/api/approvals/")) {
    approvals = approvals.filter((a) => a.id !== route.split("/")[3]);
    return {};
  }
  if (route === "/api/memories" && method === "GET")
    return structuredClone(memories);
  if (route === "/api/memories" && data) {
    const m: Memory = {
      id: crypto.randomUUID(),
      content: data.content,
      kind: data.kind as Memory["kind"],
      tags: (body as { tags: string[] }).tags,
      created_at: stamp,
      task_id: null,
    };
    memories.unshift(m);
    return m;
  }
  if (route === "/api/artifacts")
    return [
      {
        id: "demo-artifact",
        task_id: "demo-completed",
        path: "research/architecture.md",
        media_type: "text/markdown",
        created_at: stamp,
      },
    ] as Artifact[];
  if (route === "/api/events/recent")
    return [
      {
        id: 1,
        type: "task.started",
        task_id: "demo-research",
        data: {},
        created_at: stamp,
      },
      {
        id: 2,
        type: "approval.requested",
        task_id: "demo-report",
        data: {},
        created_at: stamp,
      },
    ] as KernelEvent[];
  if (route === "/api/conversations") return structuredClone(conversations);
  if (route.startsWith("/api/conversations/"))
    return structuredClone(
      messages.filter((m) => m.conversation_id === route.split("/")[3]),
    );
  if (route === "/api/chat" && data) {
    let c = conversations.find((c) => c.id === data.conversation_id);
    if (!c) {
      c = {
        id: crypto.randomUUID(),
        title: data.content.slice(0, 30),
        created_at: stamp,
      };
      conversations.unshift(c);
    }
    const newMessages: Message[] = [
      {
        id: crypto.randomUUID(),
        conversation_id: c.id,
        role: "user",
        content: data.content,
        status: "COMPLETED",
        created_at: stamp,
      },
      {
        id: crypto.randomUUID(),
        conversation_id: c.id,
        role: "assistant",
        content:
          "这是桌面示例，不调用真实模型。连接 Kernel 后，你可以在这里讨论目标，或将明确的目标交给 Agent 执行。",
        status: "COMPLETED",
        created_at: stamp,
      },
    ];
    messages.push(...newMessages);
    return { conversation: c, messages: newMessages };
  }
  throw new Error("示例模式暂不支持此操作");
}
