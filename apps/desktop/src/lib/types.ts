export type TaskStatus =
  | "PENDING"
  | "RUNNING"
  | "WAITING_HUMAN"
  | "PAUSED"
  | "COMPLETED"
  | "FAILED"
  | "CANCELLED";
export interface Task {
  id: string;
  title: string;
  goal: string;
  status: TaskStatus;
  computer_id: string | null;
  created_at: string;
  updated_at: string;
  current_step: number;
  retry_count: number;
  plan: {
    goal?: string;
    steps?: { id: string; description: string; status: string }[];
  };
  checkpoint: {
    result?: string;
    reason?: string;
    detail?: string;
    [key: string]: unknown;
  };
}
export interface Computer {
  id: string;
  name: string;
  kind: "linux" | "android";
  status: string;
  control: "AGENT" | "HUMAN" | "PAUSED";
  state: Record<string, unknown>;
}
export interface Action {
  id: string;
  task_id: string;
  spec: Record<string, unknown>;
  status: string;
  risk: string;
  result: Record<string, unknown>;
}
export interface Approval {
  id: string;
  status: string;
  action: Action;
}
export interface Memory {
  id: string;
  kind: "semantic" | "episodic";
  content: string;
  tags: string[];
  created_at: string;
  task_id: string | null;
}
export interface Artifact {
  id: string;
  task_id: string;
  path: string;
  media_type: string;
  created_at: string;
}
export interface KernelEvent {
  id: number;
  type: string;
  task_id: string | null;
  data: Record<string, unknown>;
  created_at: string;
}
export interface Conversation {
  id: string;
  title: string;
  created_at: string;
}
export interface Message {
  id: string;
  conversation_id: string;
  role: "user" | "assistant";
  content: string;
  status: string;
  created_at: string;
}
export interface Profile {
  base_url: string;
  computer_url: string;
  remember_token: boolean;
  has_token: boolean;
  platform: string;
}
export interface ConnectionInput {
  baseUrl: string;
  computerUrl: string;
  token: string;
  rememberToken: boolean;
}
export type Page =
  "dashboard" | "chat" | "tasks" | "computer" | "files" | "memory" | "settings";
