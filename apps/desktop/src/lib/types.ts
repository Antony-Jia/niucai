export type TaskStatus =
  | "PENDING"
  | "RUNNING"
  | "WAITING_HUMAN"
  | "WAITING_BOT"
  | "PAUSED"
  | "COMPLETED"
  | "FAILED"
  | "CANCELLED";
export interface Task {
  id: string;
  conversation_id?: string | null;
  title: string;
  goal: string;
  status: TaskStatus;
  computer_id: string | null;
  created_at: string;
  updated_at: string;
  current_step: number;
  retry_count: number;
  heartbeat_at?: string | null;
  progress?: TaskProgress;
  plan: {
    goal?: string;
    steps?: { id: string; description: string; status: string }[];
  };
  checkpoint: {
    result?: string;
    reason?: string;
    detail?: string;
    report?: TaskReport;
    [key: string]: unknown;
  };
}
export interface TaskReport {
  status: string;
  reason?: string | null;
  summary?: string;
  visited: string[];
  files: { id: string; path: string; media_type: string }[];
  written: string[];
  downloads: { command: string; exit_code: number | null }[];
  actions: number;
  failures: string[];
}
export type TaskOperation =
  | "pause"
  | "resume"
  | "retry"
  | "cancel"
  | "attach_computer"
  | "approve"
  | "deny"
  | "reconcile";
export interface TaskProgress {
  phase: string;
  message: string;
  wait_reason: string | null;
  last_progress_at: string;
  allowed_operations: TaskOperation[];
  action_id: string | null;
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
  /** card / goal 由 Kernel 在卡片结束、Goal 总结时自动写入。 */
  kind: "semantic" | "episodic" | "card" | "goal";
  content: string;
  tags: string[];
  created_at: string;
  task_id: string | null;
  ref_id?: string | null;
  bot_id?: string | null;
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
  updated_at?: string;
  agent_id?: string;
  computer_id?: string | null;
  settings?: ConversationSettings;
}
export interface ConversationSettings {
  /** agent：可调用工具执行；chat：只对话，不调用工具 */
  mode: "agent" | "chat" | "goal";
  /** YOLO：会话内电脑操作 / 远程任务免人工审批（shell 始终需要审批） */
  autonomy: { actions: boolean; remote: boolean };
}
export interface Message {
  id: string;
  conversation_id: string;
  role: "user" | "assistant" | "bot";
  sender_agent_id?: string | null;
  content: string;
  status: string;
  created_at: string;
  sequence?: number;
  task_id?: string | null;
  client_id?: string | null;
  delivered_at?: string | null;
}
export interface TimelineItem {
  id: string;
  cursor: number;
  type: string;
  task_id: string | null;
  created_at: string;
  data: Record<string, unknown>;
  message?: Message;
}
export interface Timeline {
  items: TimelineItem[];
  next_cursor: number;
  has_more: boolean;
  tasks: Task[];
  artifacts: Artifact[];
}
export interface Profile {
  kernel_instance_id?: string;
  computer_id?: string;
  name?: string;
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
  name?: string;
}
export interface ConnectionList {
  active: string;
  connections: Profile[];
}
export type ProbeStatus = "online" | "offline" | "auth" | "no_token";
export interface ConnectionProbe {
  key: string;
  status: ProbeStatus;
  error?: string | null;
}
export const connectionKey = (p: Profile) => p.kernel_instance_id || p.base_url;
export type Page =
  | "kanban"
  | "board"
  | "goals"
  | "bots"
  | "extensions"
  | "remote"
  | "home"
  | "dashboard"
  | "chat"
  | "tasks"
  | "task"
  | "computer"
  | "devices"
  | "files"
  | "memory"
  | "settings";
