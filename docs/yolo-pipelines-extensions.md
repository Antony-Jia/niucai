# YOLO、对话模式、依赖流程与技能/MCP

2026-10-22 实现记录。本文描述已进入源码的行为；自动化测试见文末，真实模型与真实 VPS 的联合验收尚未完成。

## 1. 会话设置：执行 / 对话模式与 YOLO

会话新增持久字段 `conversations.settings`（迁移 `a3d21022d007`）：

```json
{"mode": "agent", "autonomy": {"actions": false, "remote": false}}
```

- `mode=agent`（默认）：按原有方式执行，模型可提出动作。
- `mode=chat`：对话模式。Kernel 在每轮模型调用时不提供任何工具，并注入说明，模型只能直接作答；需要操作电脑时应提示用户切回执行模式。
- `autonomy.actions`：YOLO 电脑操作。HIGH 风险动作（click、fill、files.write、非只读 MCP 工具等）由会话设置代替人工批准。
- `autonomy.remote`：YOLO 远程任务。主 Pi 提交的远程工作包直接进入队列，不再等待人工批准。

边界：

- Kernel 总开关 `NIUCAI_YOLO_ENABLED` 默认为 `false`。关闭时任何会话的 YOLO 都不生效，开启请求返回 409。
- `shell.exec` 永远不在 YOLO 范围内，仍需逐次人工审批；Shell 本身仍受 `NIUCAI_ALLOW_SHELL` 控制。
- YOLO 不跳过审计。自动批准仍写入 Approval（`APPROVED`，备注“YOLO 自动批准”）和 Audit（`AUTO_APPROVED`, `policy=yolo`），并发送 `approval.auto_approved` 事件。远程工作包记录 `approve` 事件，`policy=yolo`。
- YOLO 不改变控制权检查：HUMAN 接管、PAUSED、租约和 run token 校验照常生效。
- 委派与流程阶段创建的执行会话继承来源会话的模式与 YOLO 设置。

接口：

| Method | Path | 行为 |
|---|---|---|
| GET | /api/conversations/{id} | 返回会话及规范化后的 `settings` |
| PATCH | /api/conversations/{id} | body `{"settings": {...}}` 局部合并，例如 `{"settings":{"autonomy":{"actions":true}}}`；发出 `conversation.settings_updated` |
| POST | /api/conversations | 可选 `settings` |

`GET /api/kernel/diagnostics` 的 `features` 增加 `yolo`、`mcp`、`shell`，客户端据此决定是否显示开关。

## 2. Bot 与 VPS 绑定

Bot 定义中的 `resource_policy` 可选择固定资源节点：

```json
{"resource_policy": {"strategy": "fixed", "fixed_node_id": "<remote-node-id>"}}
```

- 保存时校验节点存在。Context Package 的 `identity.bound_node` 给出节点名称、是否在线（以心跳和租约判断）和用法提示。
- 固定资源 Bot 不能在 Kernel 本机执行 `shell.exec`/`files.*`，必须通过 `remote_pi_submit` 把工作交给绑定节点。远程提交不能改选其它节点。
- 节点离线时工作包按 `offline_wait_seconds` 排队等待，不会伪造在线状态。
- 每个 Bot 可以绑定不同节点，因此一个流程的不同阶段可以分别在不同 VPS 上执行。

## 3. 依赖流程（多 Bot 协同）

在原有委派与 Workflow 之上新增依赖流程：一次把任务拆成最多 12 个阶段，每个阶段指定 Bot，并用 `depends_on` 声明依赖。

```json
POST /api/workflows
{
  "goal": "抓取竞品价格并出报告",
  "agent_id": "main",
  "stages": [
    {"key": "crawl", "agent_id": "crawler", "goal": "抓取三家商品价格", "output_requirements": "CSV 产物"},
    {"key": "report", "agent_id": "analyst", "goal": "基于价格数据写对比报告", "depends_on": ["crawl"]}
  ]
}
```

规则：

- `key` 匹配 `^[a-zA-Z0-9][a-zA-Z0-9_-]{0,39}$`，在流程内唯一。依赖必须存在且无环。`stages` 不能与 `routes`/`final_agent_ids` 同时使用。
- 没有依赖的阶段立即排队；有依赖的阶段处于 `WAITING_BOT`，并发出 `work_item.blocked`。
- 全部上游 `COMPLETED` 后才放行。Kernel 把上游结论（每个最多 8000 字符，超出截断）、上游产物路径与本阶段目标合成为一条 `ASSIGNMENT` 消息，并把上游产物引用并入本阶段。随后发出 `work_item.unblocked` 和 `task.resumed`（`reason=pipeline dependencies settled`）。等待上游的时间不计入本阶段的答复期限。
- 任一上游 `FAILED`/`CANCELLED` 时，下游阶段被级联取消（`work_item.blocked`，`reason=upstream stage did not complete`）。
- 流程记录在 `workflow.limits.stages`：`key`、`agent_id`、`work_item_id`、`task_id`、`conversation_id`、`handoff_id`、`depends_on`。创建时发出 `workflow.pipeline_created`。
- 不传 `origin_conversation_id` 时，Kernel 创建一个只汇总结论的概览会话，它本身不启动执行。

Bot 也可以在执行中调用 Pi 工具 `bot_pipeline` 自行拆分。返回的各阶段 handoff 可以交给 `bot_wait` 等待；该工具同样受 Bot 的工具白名单、交接跳数和流程上限约束。

## 4. 技能与 MCP

所有 Bot 共享 Kernel 统一维护的技能库和 MCP 服务器。Bot 定义中的 `skills`、`mcp_servers` 为 fnmatch 白名单：`null` 表示全部已启用项，`[]` 表示不使用。

技能：

- `/api/skills` 提供 GET/POST，`/api/skills/{id}` 提供 GET/PATCH/DELETE。字段为 `name`、`description`、`content`、`enabled`，修改时 `version` 递增。
- Context Package 只列出技能目录（名称与描述）。模型通过 `skills.read` 动作读取正文；该动作为 LOW 风险，所有启用的 Bot 默认可用。

MCP：

- 总开关为 `NIUCAI_MCP_ENABLED`（默认 `false`），单次调用超时为 `NIUCAI_MCP_TIMEOUT`（默认 60 秒）。
- 接口：
  - `GET /api/mcp/servers` 返回 `{enabled, servers}`
  - `POST /api/mcp/servers`
  - `PATCH`/`DELETE /api/mcp/servers/{id}`
  - `POST /api/mcp/servers/{id}/refresh` 刷新工具清单并记录 `status`
- 传输方式：
  - `stdio`：`command`、`args`、`env`、`cwd`
  - `http`：`url`、`headers`；Streamable HTTP，JSON 或 SSE 响应
- `env`/`headers` 的值在响应中显示为 `***`。PATCH 时传回 `***` 会保留原值。
- 模型通过 `mcp.call {server, tool, arguments}` 调用。只有工具声明 `readOnlyHint=true`，且服务器开启 `auto_approve_read_only` 时为 LOW；其余均为 HIGH，需要审批或会话 YOLO。
- 调用由 Kernel 执行，不占用电脑适配器，结果文本会截断后返回模型。

> stdio MCP 服务器以 API/Worker 进程身份在容器内启动，**不是安全沙箱**。只添加可信的服务器，不要在含生产凭据的 Kernel 上运行来源不明的命令。

## 5. 桌面客户端

- 会话页顶部提供“会话模式”（执行/对话），执行模式下还有“YOLO 电脑操作免审批”“YOLO 远程任务免审批”两个开关。Kernel 未开启 YOLO 时开关不可用并显示原因。新会话在首条消息发送前应用所选设置。
- 审批卡片、动作记录和时间线改为中文可读描述，不再显示原始 JSON；fake 适配器结果标注为“测试用，非真实执行”。
- Bot 页：
  - 显示每个 Bot 的绑定节点及其状态
  - 可配置可用技能与可用 MCP 服务器
  - 原 JSON 编辑收进“高级配置”
  - 新增“依赖流程”表单，可添加阶段、选择 Bot 和勾选依赖
  - 流程列表显示阶段链
- 新增“扩展”页，用于管理技能库和 MCP 服务器（stdio/http 配置、刷新、工具只读标记）。

## 验证

- Kernel：`tests/test_autonomy.py`、`tests/test_pipeline.py`、`tests/test_extensions.py` 覆盖以下行为：
  - YOLO 总开关、shell 例外、远程工作包自动入队、设置继承
  - 依赖放行与上游结论注入、级联取消、环检测
  - 技能/MCP 白名单、风险分级、密钥遮蔽，以及 stdio 往返调用（`tests` 内置 MCP 脚本）
- Desktop：`tests/describe.test.tsx` 覆盖可读描述、无 JSON 渲染，以及示例模式下的 YOLO 与对话模式设置。
- 未完成：真实模型下的 `bot_pipeline` 拆分质量、真实 VPS 节点上的跨阶段产物交接，以及第三方 MCP 服务器兼容性。
