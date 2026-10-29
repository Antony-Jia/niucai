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
const MUTATING = new Set(["card_progress", "card_comment", "card_propose", "card_block", "card_submit"]);

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
    Type.Object({ body: Type.String(), card_id: ID }),
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
    "需要用户或 kernel 回答才能继续时提问；随后结束本次执行，回答后会重新开始执行。",
    Type.Object({
      question: Type.String(),
      mention: Type.Optional(Type.Union([Type.Literal("user"), Type.Literal("kernel")])),
    }),
  ],
  [
    "card_submit",
    "提交当前卡片的结果摘要；交付文件写在工作区内。提交后结束本次执行。",
    Type.Object({ summary: Type.String() }),
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
            terminate: ENDS_ATTEMPT.has(name),
          };
        },
      }),
    );
  }
}
