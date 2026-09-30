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
} from "./types";
import { demoRequest, resetDemo } from "./demo";

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
  if (!native())
    throw new Error("请使用 Windows 桌面客户端连接 Kernel，或先体验示例。");
  return invoke<T>("api_request", { path, method, body: body ?? null });
}
export async function loadProfile(): Promise<Profile> {
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
  if (!native()) throw new Error("连接真实服务器需要桌面客户端。");
  return invoke("connect_kernel", { input });
}
export async function disconnect() {
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
  actions: (id: string) => request<Action[]>(`/api/tasks/${id}/actions`),
  computers: () => request<Computer[]>("/api/computers"),
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
  events: () => request<KernelEvent[]>("/api/events/recent?limit=100"),
  conversations: () => request<Conversation[]>("/api/conversations"),
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
export async function closeComputer() {
  if (native()) await invoke("close_computer");
}
export async function toggleComputerFullscreen() {
  if (native()) await invoke("toggle_computer_fullscreen");
}
export async function downloadArtifact(id: string) {
  if (demo) throw new Error("示例文件不提供实际下载。");
  return invoke<string | null>("download_artifact", { artifactId: id });
}
