// 把动作规格、动作结果与时间线事件翻译成可读文字，界面不再直接展示 JSON。

export interface Field {
  label: string;
  value: string;
  /** 代码、路径、命令等等宽显示 */
  code?: boolean;
  /** 长文本（正文、输出），折叠到可滚动块中 */
  long?: boolean;
}
export interface Described {
  title: string;
  summary: string;
  fields: Field[];
}

type Spec = Record<string, unknown>;

const LONG = 160;

/** 任意值转成一行或多行可读文本（嵌套对象按“键：值”展开）。 */
export function plain(value: unknown, depth = 0): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "boolean") return value ? "是" : "否";
  if (typeof value === "string" || typeof value === "number")
    return String(value);
  if (Array.isArray(value))
    return value.length
      ? value.map((v) => plain(v, depth + 1)).join("、")
      : "无";
  if (typeof value === "object") {
    const entries = Object.entries(value as Spec);
    if (!entries.length) return "无";
    const sep = depth === 0 ? "\n" : "；";
    return entries.map(([k, v]) => `${k}：${plain(v, depth + 1)}`).join(sep);
  }
  return String(value);
}

function field(label: string, value: unknown, code = false): Field {
  const text = plain(value);
  return {
    label,
    value: text,
    code,
    long: text.length > LONG || text.includes("\n"),
  };
}

function clip(text: string, length = 60) {
  return text.length > length ? `${text.slice(0, length)}…` : text;
}

export function describeAction(spec: Spec): Described {
  const type = String(spec.type ?? "");
  const s = (key: string) => String(spec[key] ?? "");
  switch (type) {
    case "browser.navigate":
      return {
        title: "打开网页",
        summary: s("url"),
        fields: [field("网址", spec.url, true)],
      };
    case "browser.snapshot":
      return {
        title: "读取页面内容",
        summary: "获取当前页面的文字与可操作元素",
        fields: [],
      };
    case "browser.click":
      return {
        title: "点击页面元素",
        summary: `元素 ${s("ref")}${spec.button === "right" ? "（右键）" : ""}`,
        fields: [field("元素", spec.ref, true)],
      };
    case "browser.fill":
      return {
        title: "填写输入框",
        summary: `在元素 ${s("ref")} 中输入 ${s("text").length} 个字符`,
        fields: [field("元素", spec.ref, true), field("内容", spec.text)],
      };
    case "browser.scroll":
      return {
        title: "滚动页面",
        summary: `${Number(spec.y) >= 0 ? "向下" : "向上"} ${Math.abs(Number(spec.y) || 0)} 像素`,
        fields: [],
      };
    case "browser.screenshot":
      return { title: "页面截图", summary: "保存当前页面截图", fields: [] };
    case "files.read":
      return {
        title: "读取文件",
        summary: s("path"),
        fields: [field("路径", spec.path, true)],
      };
    case "files.write":
      return {
        title: "写入文件",
        summary: `${s("path")} · ${s("content").length} 个字符`,
        fields: [
          field("路径", spec.path, true),
          field("内容", spec.content, true),
        ],
      };
    case "shell.exec": {
      const argv = Array.isArray(spec.argv) ? spec.argv.map(String) : [];
      return {
        title: "执行命令",
        summary: clip(argv.join(" ")),
        fields: [field("命令", argv.join(" "), true)],
      };
    }
    case "skills.read":
      return {
        title: "查阅技能",
        summary: s("name"),
        fields: [field("技能", spec.name)],
      };
    case "mcp.call": {
      const args = (spec.arguments ?? {}) as Spec;
      return {
        title: "调用 MCP 工具",
        summary: `${s("server")} / ${s("tool")}`,
        fields: [
          field("服务器", spec.server),
          field("工具", spec.tool, true),
          ...Object.entries(args).map(([k, v]) => field(`参数 ${k}`, v)),
        ],
      };
    }
    default: {
      const { type: _type, ...rest } = spec;
      return {
        title: type || "未知动作",
        summary: "",
        fields: Object.entries(rest).map(([k, v]) => field(k, v)),
      };
    }
  }
}

const RESULT_LABELS: Record<string, string> = {
  url: "网址",
  title: "页面标题",
  text: "页面文字",
  observation: "观察",
  path: "文件",
  bytes: "大小（字节）",
  media_type: "类型",
  content: "内容",
  output: "输出",
  exit_code: "退出码",
  version: "版本",
  name: "名称",
  server: "服务器",
  tool: "工具",
  structured: "结构化结果",
  error: "错误",
  detail: "详情",
};
// 页面元素列表等仅供模型使用的字段不在界面展开。
const HIDDEN = new Set([
  "refs",
  "elements",
  "truncated",
  "is_error",
  "adapter",
  "type",
  "ok",
]);

export function describeResult(result: unknown): Field[] {
  if (result === null || result === undefined) return [];
  if (typeof result !== "object" || Array.isArray(result))
    return [field("结果", result)];
  const r = result as Spec;
  const fields: Field[] = [];
  for (const [key, value] of Object.entries(r)) {
    if (HIDDEN.has(key) || value === undefined) continue;
    const code = ["url", "path", "output"].includes(key);
    fields.push(field(RESULT_LABELS[key] ?? key, value, code));
  }
  if (Array.isArray(r.elements))
    fields.push(field("可操作元素", `${r.elements.length} 个`));
  if (r.truncated) fields.push(field("提示", "内容过长，已截断"));
  if (r.is_error) fields.push(field("工具报告", "调用返回错误"));
  if (r.adapter === "fake")
    fields.push(field("适配器", "fake（测试用，非真实执行）"));
  return fields;
}

export const actionStatus: Record<string, string> = {
  PROPOSED: "已提出",
  WAITING_APPROVAL: "等待审批",
  APPROVED: "已批准",
  EXECUTING: "执行中",
  SUCCEEDED: "已完成",
  FAILED: "失败",
  DENIED: "已拒绝",
  UNKNOWN: "结果待确认",
};

export const riskLabel: Record<string, string> = {
  LOW: "低风险",
  MEDIUM: "中风险",
  HIGH: "高风险",
  CRITICAL: "极高风险",
};

const EVENTS: Record<string, string> = {
  "task.created": "已创建执行",
  "task.started": "开始执行",
  "task.pending": "排队中",
  "task.completed": "执行完成",
  "task.failed": "执行失败",
  "task.waiting_human": "等待你处理",
  "task.pause": "已暂停",
  "task.paused": "已暂停",
  "task.resume": "已恢复",
  "task.resumed": "已恢复",
  "task.cancel": "已取消",
  "task.cancelled": "已取消",
  "task.retry": "重新执行",
  "task.waiting_approval": "等待审批",
  "task.recovered": "已恢复执行",
  "task.step_completed": "完成一个步骤",
  "action.executing": "正在执行动作",
  "action.succeeded": "动作完成",
  "action.failed": "动作失败",
  "action.unknown": "动作结果待确认",
  "action.denied": "动作被拒绝",
  "approval.requested": "等待你审批",
  "approval.auto_approved": "YOLO 自动批准",
  "work_item.created": "分派协作工作",
  "work_item.blocked": "上游未完成，本阶段已取消",
  "work_item.unblocked": "上游已完成，开始本阶段",
  "work_item.cancelled": "协作工作已取消",
  "workflow.created": "创建协作流程",
  "workflow.pipeline_created": "创建依赖流程",
  "workflow.failed": "协作流程失败",
  "conversation.settings_updated": "会话设置已更新",
};

const REASONS: Record<string, string> = {
  approval_decided: "审批已处理",
  "bot dependencies settled": "协作结果已返回",
  "pipeline dependencies settled": "上游阶段已完成",
  "upstream stage did not complete": "上游阶段未完成",
};

/** 时间线事件的可读标题；返回 null 表示不在会话中展示。 */
export function eventLabel(type: string, data: Spec = {}): string | null {
  const base = EVENTS[type];
  if (!base) return null;
  const parts = [base];
  if (type === "workflow.pipeline_created" && Array.isArray(data.stages))
    parts.push(data.stages.map(String).join(" → "));
  if (type === "conversation.settings_updated") {
    const settings = (data.settings ?? {}) as {
      mode?: string;
      autonomy?: Record<string, boolean>;
    };
    parts.push(settingsSummary(settings));
  }
  const reason = typeof data.reason === "string" ? data.reason : "";
  if (reason) parts.push(REASONS[reason] ?? reason);
  return parts.join(" · ");
}

export function settingsSummary(settings: {
  mode?: string;
  autonomy?: Record<string, boolean>;
}) {
  const yolo = [
    settings.autonomy?.actions && "操作免审批",
    settings.autonomy?.remote && "远程免审批",
  ].filter(Boolean);
  return `${settings.mode === "chat" ? "对话模式" : "执行模式"}${yolo.length ? ` · YOLO（${yolo.join("、")}）` : ""}`;
}
