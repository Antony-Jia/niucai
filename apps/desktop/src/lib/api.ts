import { invoke, isTauri } from "@tauri-apps/api/core";
import type {
  Artifact,
  Computer,
  Conversation,
  KernelEvent,
  Memory,
  Message,
  Profile,
  Task,
  Approval,
  ConnectionInput,
  Timeline,
} from "./types";
import { demoRequest, resetDemo } from "./demo";

export interface BrowserBridge {
  request<T>(path: string, method?: string, body?: unknown): Promise<T>;
  loadProfile(): Promise<Profile>;
  connect(input: ConnectionInput): Promise<Profile>;
  disconnect(): Promise<void>;
  downloadArtifact(id: string): Promise<string | null>;
}
let browserBridge: BrowserBridge | undefined;
export function configureBrowser(bridge: BrowserBridge) {
  browserBridge = bridge;
}
let demo = false;
export const native = () => isTauri();
export const isDemo = () => demo;
export function setDemo(value: boolean) {
  demo = value;
  if (value) resetDemo();
}

export async function request<T>(
  path: string,
  method = "GET",
  body?: unknown,
): Promise<T> {
  if (demo) return demoRequest(path, method, body) as Promise<T>;
  if (browserBridge) return browserBridge.request<T>(path, method, body);
  if (!native())
    throw new Error("请使用 Windows 桌面客户端连接 Kernel，或先体验示例。");
  return invoke<T>("api_request", { path, method, body: body ?? null });
}
export async function loadProfile(): Promise<Profile> {
  if (browserBridge) return browserBridge.loadProfile();
  if (!native())
    return {
      base_url: "",
      computer_url: "",
      remember_token: false,
      has_token: false,
      platform: "preview",
    };
  return invoke("load_profile");
}
export async function connect(input: ConnectionInput): Promise<Profile> {
  if (browserBridge) return browserBridge.connect(input);
  if (!native()) throw new Error("连接真实服务器需要桌面客户端。");
  return invoke("connect_kernel", { input });
}
export async function disconnect() {
  if (browserBridge) await browserBridge.disconnect();
  if (native()) await invoke("disconnect_kernel");
  demo = false;
}
export const api = {
  tasks: () => request<Task[]>("/api/tasks?limit=200"),
  task: (id: string) => request<Task>(`/api/tasks/${id}`),
  createTask: (title: string, goal: string, computerId?: string) =>
    request<Task>("/api/tasks", "POST", {
      title,
      goal,
      computer_id: computerId || null,
    }),
  taskControl: (id: string, operation: string) =>
    request<Task>(`/api/tasks/${id}/${operation}`, "POST"),
  attachTaskComputer: (id: string, computerId: string) =>
    request<Task>(`/api/tasks/${id}/computer`, "PUT", {
      computer_id: computerId,
    }),
  actions: (id: string) => request<Action[]>(`/api/tasks/${id}/actions`),
  computers: () => request<Computer[]>("/api/computers"),
  computerPreview: (id: string) =>
    request<{ image: string; captured_at: string }>(
      `/api/computers/${id}/preview`,
    ),
  computerControl: (id: string, operation: string) =>
    request<Computer>(`/api/computers/${id}/${operation}`, "POST"),
  registerComputer: (name: string) =>
    request<Computer>("/api/computers", "POST", { name, kind: "linux" }),
  approvals: () => request<Approval[]>("/api/approvals"),
  decide: (id: string, approve: boolean) =>
    request(`/api/approvals/${id}/${approve ? "approve" : "deny"}`, "POST", {
      note: "",
    }),
  reconcile: (id: string, outcome: "SUCCEEDED" | "FAILED") =>
    request(`/api/actions/${id}/reconcile`, "POST", { outcome }),
  memories: () => request<Memory[]>("/api/memories?limit=200"),
  createMemory: (content: string, kind: string, tags: string[]) =>
    request<Memory>("/api/memories", "POST", { content, kind, tags }),
  artifacts: () => request<Artifact[]>("/api/artifacts?limit=200"),
  taskArtifacts: (id: string) =>
    request<Artifact[]>(`/api/tasks/${id}/artifacts`),
  artifactPreview: (id: string) =>
    request<{ image: string }>(`/api/artifacts/${id}/preview`),
  events: () => request<KernelEvent[]>("/api/events/recent?limit=100"),
  conversations: () => request<Conversation[]>("/api/conversations"),
  createConversation: (computerId?: string) =>
    request<Conversation>("/api/conversations", "POST", {
      computer_id: computerId || null,
    }),
  sendMessage: (id: string, content: string, clientId: string) =>
    request<{
      conversation: Conversation;
      messages: Message[];
      task: Task;
    }>(`/api/conversations/${id}/messages`, "POST", {
      content,
      client_id: clientId,
    }),
  timeline: async (id: string): Promise<Timeline> => {
    let after = 0;
    const items: Timeline["items"] = [];
    while (true) {
      const page = await request<Timeline>(
        `/api/conversations/${id}/timeline?after=${after}&limit=200`,
      );
      items.push(...page.items);
      if (!page.has_more) return { ...page, items };
      if (page.next_cursor <= after) throw new Error("会话时间线游标未前进");
      after = page.next_cursor;
    }
  },
  messages: (id: string) =>
    request<Message[]>(`/api/conversations/${id}/messages`),
  chat: (content: string, conversationId?: string) =>
    request<{ conversation: Conversation; messages: Message[] }>(
      "/api/chat",
      "POST",
      { content, conversation_id: conversationId || null },
    ),
};
import type { Action } from "./types";
export async function openComputer(id: string) {
  if (demo) return;
  return invoke("open_computer", { computerId: id });
}
let computerQueue: Promise<unknown> = Promise.resolve();
function computerCommand(command: string, args?: Record<string, unknown>) {
  const result = computerQueue
    .catch(() => {})
    .then(
      () =>
        new Promise((resolve, reject) => {
          const timer = setTimeout(
            () => reject(new Error("桌面窗口响应超时，请重试连接")),
            8000,
          );
          invoke(command, args)
            .then(resolve, reject)
            .finally(() => clearTimeout(timer));
        }),
    );
  computerQueue = result;
  return result;
}
export async function closeComputer() {
  if (native()) await computerCommand("close_computer");
}
export async function changeComputerControl(
  id: string,
  operation: string,
  onCleanupError: (message: string) => void,
) {
  // Ownership must reach Kernel even when the local desktop is broken.
  const computer = await api.computerControl(id, operation);
  if (operation !== "take-control") {
    void closeComputer().catch(() =>
      onCleanupError(
        "控制权已更新，但桌面窗口关闭失败，请关闭残留窗口后重新连接。",
      ),
    );
  }
  return computer;
}
export function embedComputer(
  computerId: string,
  bounds: {
    x: number;
    y: number;
    width: number;
    height: number;
    visible: boolean;
  },
) {
  return computerCommand("embed_computer", { computerId, bounds });
}
export async function toggleComputerFullscreen() {
  if (native()) await invoke("toggle_computer_fullscreen");
}
export async function downloadArtifact(id: string) {
  if (demo) throw new Error("示例文件不提供实际下载。");
  if (browserBridge) return browserBridge.downloadArtifact(id);
  return invoke<string | null>("download_artifact", { artifactId: id });
}
