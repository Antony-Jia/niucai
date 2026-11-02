// Pi 的白板工具（niucai-board MCP 的 Pi 对应物）：经本作业的 Runner 代理调用 Kernel。
// NIUCAI_BOARD_ADDRESS 是 Unix socket 路径（Linux）或 tcp:127.0.0.1:<port>（Windows，带每作业密钥）；
// 扩展不持有节点令牌与板令牌。工具名与参数须与 board_mcp.py 保持一致（测试会核对）。
import { randomUUID } from "node:crypto";
import { createConnection } from "node:net";
import { Type } from "@earendil-works/pi-ai";
import { defineTool, type ExtensionAPI } from "@earendil-works/pi-coding-agent";

const ID = Type.Optional(Type.String({ description: "卡片 id，省略时为当前卡片" }));
// 提交或提问后本次执行结束：跳过模型的自动续写，Runner 按工具结果收尾。
const ENDS_ATTEMPT = new Set(["card_block", "card_submit"]);
// card_wait（含 card_delegate 的 wait=true）仅在真的进入等待时结束本次执行；条件已满足则把结果交回继续干活。
const WAITS = new Set(["card_wait", "card_delegate"]);
const MUTATING = new Set(["card_progress", "card_comment", "card_propose", "card_block", "card_submit", "artifact_put", "card_claim", "card_delegate", "card_wait"]);

const TOOLS = [
  ["board_view", "列出同一目标下的所有卡片（id、标题、状态、负责 Bot、摘要）。", Type.Object({})],
  ["card_get", "读取一张卡片的目标、验收标准、状态与最近 20 条动态。", Type.Object({ card_id: ID })],
  ["inbox", "读取 @ 提及当前 Bot 的新消息（读过即标记，不会重复返回）。", Type.Object({})],
  [
    "card_progress",
    "在当前卡片上写一条进度，可附 0-100 的百分比。",
    Type.Object({ text: Type.String(), percent: Type.Optional(Type.Number()) }),
  ],
  [
    "card_comment",
    "在当前或同目标的另一张卡片上发评论，正文里用 @名称 提及其他 Bot。",
    Type.Object({
      body: Type.String(),
      card_id: ID,
      reply_to: Type.Optional(Type.Integer({ description: "回复的动态（事件）编号，用于接上某条评论的上下文" })),
    }),
  ],
  [
    "card_propose",
    "提议拆出一张新卡片，由用户或编排者决定是否创建。",
    Type.Object({
      title: Type.String(),
      goal: Type.Optional(Type.String()),
      acceptance: Type.Optional(Type.String()),
      bot: Type.Optional(Type.String({ description: "建议的负责 Bot 名称" })),
      tags: Type.Optional(Type.Array(Type.String())),
    }),
  ],
  [
    "card_block",
    "需要用户或 kernel 回答才能继续时提问；随后结束本次执行，回答后会重新开始执行。" +
      "取舍类问题（decision，默认）会先经裁判：用户以前表过态的会代答，问错了的会退回说明，没有先例才转给用户。" +
      "权限档位、开工审批、预算追加、外部动作、凭据与账号、删除数据这类授权问题必须写 category=authorization，直接转给用户。",
    Type.Object({
      question: Type.String(),
      mention: Type.Optional(Type.Union([Type.Literal("user"), Type.Literal("kernel")])),
      category: Type.Optional(Type.Union([Type.Literal("decision"), Type.Literal("authorization")])),
    }),
  ],
  [
    "card_submit",
    "提交当前卡片的结果摘要；交付文件写在工作区内。提交后结束本次执行。",
    Type.Object({ summary: Type.String() }),
  ],
  [
    "bots_list",
    "列出同事 Bot：名称、角色、标签、环境摘要（系统/引擎/浏览器/仓库/已登录站点）、在线与忙闲、当前占用的资源。",
    Type.Object({}),
  ],
  [
    "artifact_put",
    "把工作区里的文件（path）或一段文本（text）作为产物上传，单个不超过 1 MiB；隐藏文件与密钥类文件（.env、私钥等）不能按路径上传。attach_to 可把它挂为同一目标内尚未开始的卡片的输入，对方领取时会放进 inputs/<card_id>/。",
    Type.Object({
      name: Type.Optional(Type.String()),
      path: Type.Optional(Type.String({ description: "工作区内的文件路径" })),
      text: Type.Optional(Type.String()),
      attach_to: Type.Optional(Type.String({ description: "接收输入的卡片 id" })),
    }),
  ],
  [
    "artifact_get",
    "按 artifact_id 取回同一目标内的产物，内容落到工作区 inputs/fetched/ 并返回路径；不带 artifact_id 则列出本目标的产物。",
    Type.Object({ artifact_id: Type.Optional(Type.String()) }),
  ],
  [
    "card_claim",
    "声明要使用的资源（repo:/path:/site:/desktop:/browser:/ext: 前缀的标识），mode 为 exclusive 或 shared。独占被他人占用时调用失败并告知占用者；共享重叠只提示。卡片结束时自动释放。",
    Type.Object({
      resources: Type.Optional(
        Type.Array(
          Type.Union([
            Type.String(),
            Type.Object({
              resource: Type.String(),
              mode: Type.Optional(Type.Union([Type.Literal("exclusive"), Type.Literal("shared")])),
            }),
          ]),
        ),
      ),
      resource: Type.Optional(Type.String()),
      mode: Type.Optional(Type.Union([Type.Literal("exclusive"), Type.Literal("shared")])),
    }),
  ],
  [
    "card_delegate",
    "把一件事委派给同目标的另一个 Bot：to 填 Bot 名称，或 tags 按标签挑一个；title/goal/acceptance 写清楚要什么。inputs 引用已有产物（[{artifact_id}]），claims 声明对方要用的资源。对方始终使用自己的权限档位，不能由你指定；需要审批的接收方会先停在待批准。wait=true 等于创建后立即 card_wait：本次执行结束，对方完成后自动续接并附上结果。受 collab_policy、委派深度与目标预算限制，违反时调用失败并说明原因。",
    Type.Object({
      to: Type.Optional(Type.String({ description: "接收方 Bot 名称" })),
      tags: Type.Optional(Type.Array(Type.String())),
      title: Type.String(),
      goal: Type.String(),
      acceptance: Type.Optional(Type.String()),
      inputs: Type.Optional(Type.Array(Type.Object({ artifact_id: Type.String() }))),
      claims: Type.Optional(Type.Array(Type.String())),
      wait: Type.Optional(Type.Boolean()),
    }),
  ],
  [
    "card_wait",
    "等待同一目标内的若干卡片结束（DONE/FAILED/CANCELLED）。mode=all 等全部，any 等任一；timeout 秒，默认 1800，最大 14400。条件尚未满足时本次执行结束并进入等待，满足或超时后自动续接会话；已满足则直接返回结果。不能形成环形等待。",
    Type.Object({
      card_ids: Type.Array(Type.String()),
      mode: Type.Optional(Type.Union([Type.Literal("all"), Type.Literal("any")])),
      timeout: Type.Optional(Type.Integer()),
    }),
  ],
] as const;

function call(tool: string, args: Record<string, unknown>): Promise<{ ok: boolean; result?: unknown; error?: unknown }> {
  const address = process.env.NIUCAI_BOARD_ADDRESS || "";
  const request: Record<string, unknown> = { tool, args };
  if (process.env.NIUCAI_BOARD_SECRET) request.secret = process.env.NIUCAI_BOARD_SECRET;
  return new Promise((resolve, reject) => {
    let target;
    if (address.startsWith("tcp:")) {
      const [host, port] = [address.slice(4, address.lastIndexOf(":")), address.slice(address.lastIndexOf(":") + 1)];
      if (host !== "127.0.0.1") return reject(new Error("board proxy must be on loopback"));
      target = { host, port: Number(port) };
    } else {
      target = { path: address };
    }
    const socket = createConnection(target);
    let buffer = "";
    socket.setEncoding("utf8");
    socket.setTimeout(60000, () => socket.destroy(new Error("board proxy timeout")));
    socket.on("connect", () => socket.write(JSON.stringify(request) + "\n"));
    socket.on("data", chunk => {
      buffer += chunk;
      if (buffer.endsWith("\n")) socket.end();
    });
    socket.on("error", error => reject(new Error(`runner unreachable: ${error.message}`)));
    socket.on("close", () => {
      if (!buffer) return reject(new Error("runner closed the connection"));
      try {
        resolve(JSON.parse(buffer));
      } catch {
        reject(new Error("malformed board reply"));
      }
    });
  });
}

function waitsNow(result: unknown): boolean {
  return typeof result === "object" && result !== null && "next" in result && !(result as { ready?: boolean }).ready;
}

export default function (pi: ExtensionAPI) {
  if (!process.env.NIUCAI_BOARD_ADDRESS) return; // 非卡片作业没有白板
  for (const [name, description, parameters] of TOOLS) {
    pi.registerTool(
      defineTool({
        name,
        label: name,
        description,
        parameters,
        async execute(_id, params) {
          const args: Record<string, unknown> = { ...params };
          // Runner 转发失败会重试；同一次调用带同一个幂等键，Kernel 不会重复记事件。
          if (MUTATING.has(name)) args.idempotency_key = randomUUID().replace(/-/g, "");
          const reply = await call(name, args);
          if (!reply.ok) throw new Error(String(reply.error ?? "board call failed"));
          return {
            content: [{ type: "text", text: JSON.stringify(reply.result) }],
            details: undefined,
            terminate: ENDS_ATTEMPT.has(name) || (WAITS.has(name) && waitsNow(reply.result)),
          };
        },
      }),
    );
  }
}
