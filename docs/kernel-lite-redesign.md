# 轻量 Kernel 与 Bot 白板重构方案

状态：**P1–P5 已实现并有自动化测试，P6 只完成“开关停用旧链路”这一步（删除代码与表待验收后进行），各阶段的实机验收均未完成；部分能力延后，见 §15 各阶段的“实现状态”**。本文是设计方案，文中的表、字段、接口、工具名都不代表现有能力。外部 CLI（Claude Code、Codex、Pi）的配置路径与命令行选项以实现时锁定版本的 `--help` 和实测为准。

修订：2026-10-29，初稿。

修订：2026-10-29（二），确定 §16 的七项待定问题：槽位默认 1 且可调大、默认不验收、预览直连节点、新增简单 Memory 与执行历史留存、Windows Runner 由 Desktop 以内置程序拉起、无节点时不提供默认 Bot、Pi 通用 MCP 桥接延后。

相关文档：[远程 Pi 执行节点 V1](remote-pi-nodes.md)、[远程节点多执行引擎](remote-agent-engines.md)、[多 Bot 协作方案](multi-bot-collaboration.md)、[YOLO、依赖流程与技能/MCP](yolo-pipelines-extensions.md)。本方案落地后，上述文档中与 Kernel 内执行、Handoff、群聊调度相关的部分将被取代。

## 1. 背景：为什么要重构

当前代码里有多套互相重叠的概念：

| 概念 | 位置 | 问题 |
|---|---|---|
| Conversation / Task | `control/conversations.py`、`control/tasks.py` | 执行轮次绑定在 Kernel Worker 上 |
| Agent（Bot） | `agents` 表、`control/bots.py` | Bot 是身份，但执行在 Kernel 的 Pi 里，和节点没有关系 |
| Workflow / WorkItem / Handoff | `control/collaboration.py`（约 950 行） | 为让 Kernel 内的 Pi 互相等待，引入了 `WAITING_BOT`、跳数、群聊调度等复杂状态机 |
| RemoteNode / RemoteJob | `control/remote.py`、`services/node-runner` | 远程执行是"主 Pi 的子工作包"，不是一等任务 |
| Computer + relay | `computers`、`computer_proxy.py`、`relay/` | 浏览器由 Kernel 的 Pi 经私网中继、CDP 控制 |
| Kernel 内 Pi 运行时 | `agents/pi_adapter.py`、`pi-runtime/`、`checkpointer.py` | Kernel 既调度又推理又执行，体量重、恢复逻辑复杂 |

Desktop 端也对应出现了"会话 / 看板 / 远程 Pi / Bots / 扩展 / Settings + 左下角连接切换 + 本机控制中心 + 执行资源"多处入口，用户很难判断"设备该从哪里加、任务由谁执行"。

根本原因：**Kernel 同时承担调度、推理和执行**。

## 2. 目标与非目标

目标：

1. **Kernel 轻量化**：Kernel 只保存状态、做授权，再加一个编排 LLM；不再在 Kernel 内运行 Pi、浏览器或 Shell。
2. **Bot 是任务的唯一执行单位**：Bot = 节点 + 引擎 + 角色 + skills + MCP。VPS 上的 Pi、Windows 上的 Claude Code / Codex 都是 Bot。
3. **白板协作**：编排 LLM 把目标拆成卡片；Bot 领取卡片，并把进度、结果、产物和问题写回白板。
4. **设备连接简单**：Settings 里只有"Kernel"和"节点 / Bot"两块，点一个"＋"加一个，并显示拓扑图。
5. **Bot 便于装 MCP 和 skills**：Kernel 管清单，Runner 落到各引擎自己的配置位置。

非目标（本方案不做）：

- Bot 之间点对点通信、Bot 之间互相派发任务。
- Kernel 对 Bot 内部每一步工具调用做逐条审批（第三方 CLI 做不到，见 §11）。
- 多用户、多租户；向量记忆。
- Android Device Node（仍未实现，仍不得虚构在线状态）。

## 3. 原则（修订）

本方案修改 AGENTS.md 的核心表述：

> 旧：用户定义 Kernel，模型只提出决策，**Kernel 拥有状态与执行权**。
> 新：用户定义 Kernel，**Kernel 拥有状态与授权**；Bot 在被授权的卡片范围内执行，执行过程回传 Kernel 留档。

具体规则：

1. **单一事实来源**：卡片、评论、产物、审批都存在 Kernel 的 PostgreSQL 里。节点上的文件和会话只是缓存，可以丢。
2. **单一调度者**：只有编排 LLM 和用户能创建、分配卡片。Bot 只能"提议"子卡片。
3. **Bot 之间不直接通信**：所有协作都经过白板（卡片评论 `@Bot`），可回放、可审计、可去重。
4. **连接只从节点主动发起**：节点只出站连接 Kernel（HTTPS），家里 NAT 后的 Windows 和 VPS 用同一种方式接入。Kernel 不 SSH 进节点。
5. **授权以卡片为粒度**：开工审批、权限档位、可用 MCP 和 skills、预算都挂在卡片和 Bot 上；Bot 内部的工具调用由引擎自身的权限模式约束。
6. **能力如实上报**：节点在线只表示 Runner 进程活着；引擎和凭据是否可用以探测结果和真实卡片结果为准。

## 4. 总体架构

```
                   Desktop / 手机 PWA
                          │  REST + WebSocket（Bearer Token）
┌─────────────────────────▼──────────────────────────┐
│ Kernel（FastAPI + PostgreSQL + 1 个 Worker）        │
│  ├─ 编排 LLM（Model Gateway，直接 tool-calling）    │
│  ├─ 白板：cards / card_events / artifacts           │
│  ├─ 节点与 Bot 注册、配对、配置下发                 │
│  ├─ 审批、审计、事件流                              │
│  └─ Runner API：claim / heartbeat / events / finish │
└───────▲──────────────────▲─────────────────▲───────┘
        │ 出站 HTTPS 长轮询                   │
┌───────┴────────┐ ┌───────┴────────┐ ┌──────┴──────────────┐
│ 节点 VPS-A     │ │ 节点 VPS-B     │ │ 节点 Windows 本机    │
│ Runner (Linux) │ │ Runner (Linux) │ │ Runner (Windows)     │
│  Bot 浏览员:Pi │ │  Bot 后端:Codex│ │  Bot 本地编码:Claude │
│  + Chrome(本机 │ │                │ │  Bot 本地编码2:Codex │
│    CDP:9222)   │ │                │ │                      │
└────────────────┘ └────────────────┘ └──────────────────────┘
```

角色分工：

| 组件 | 职责 | 不做 |
|---|---|---|
| Kernel | 状态、授权、编排 LLM、事件流、产物存储 | 运行 Pi、浏览器、Shell；直连节点 |
| 编排 LLM | 拆解目标、建卡、分配、验收、问用户 | 执行任何真实动作 |
| Runner | 配对、心跳、领卡、拉起引擎进程、落地 skills/MCP、回传事件与产物、提供白板工具 | 自行决定做哪张卡 |
| Bot（引擎进程） | 在工作区内完成一张卡；通过白板工具汇报 | 给其它 Bot 派活 |

## 5. 概念模型

| 新概念 | 定义 | 对应旧概念 |
|---|---|---|
| **Node 节点** | 一台运行 Runner 的机器（VPS / Windows / Docker） | `RemoteNode`（复用） |
| **Bot** | 节点上的一个执行者：引擎 + 角色说明 + skills + MCP + 权限档位；同一时间只做一张卡 | `Agent` + `RemoteNode.capabilities.engines` 合并 |
| **Goal 目标** | 用户的一个请求，对应一次与编排 LLM 的对话，下面挂一块白板 | `Conversation` / `Workflow` |
| **Card 卡片** | 白板上的一个工作项：目标、验收标准、负责 Bot（或标签）、依赖、状态 | `WorkItem` |
| **Attempt 执行尝试** | 卡片的一次执行，有租约、会话 ID、事件序号、结果 | `RemoteJob`（复用） |
| **Card Event** | 卡片时间线：评论、状态变化、进度、产物、提议 | `Handoff` + `Message` + `Event` 的一部分 |
| **Asset** | Kernel 托管的 skill / MCP 定义，按 Bot 绑定 | `Skill`、`McpServer`（复用） |

一句话关系：**用户 → Goal → 编排 LLM → Card → Bot（在 Node 上）→ Attempt → Card Event / Artifact → 编排 LLM 验收 → 用户。**

## 6. 数据模型

原则：新建核心表，旧的执行表冻结不删，到 P6 统一下线（§14）。执行尝试直接复用 `remote_jobs`，这样 Runner 协议、租约和产物上传全部沿用。

### 6.1 `nodes`（沿用 `remote_nodes`，加列）

| 字段 | 说明 |
|---|---|
| 现有字段 | `id, name, resource_type, token_hash, enabled, heartbeat_at, capabilities, connection` |
| `platform` | `linux` / `windows`，配对时上报 |
| `slots` | 本节点同时运行的 Attempt 上限，默认 1，用户可调大（例如让 Windows 上的 Claude 和 Codex 同时跑） |
| `preview_url` | 心跳上报的画面预览地址（节点 HTTPS），没有则为空 |
| `runner_version` | 心跳上报 |
| `paired_at` | 配对完成时间 |

`capabilities` 保持心跳上报的引擎探测结果（版本、凭据是否可用、沙箱能力）。

### 6.2 `bots`（新表）

| 字段 | 说明 |
|---|---|
| `id, name` | `name` 唯一，用于 `@name` |
| `node_id` | 所在节点 |
| `engine` | `pi` / `codex` / `claude` |
| `role` | 一句话职责，供编排 LLM 选人 |
| `instructions` | 角色说明 / playbook，注入为引擎的系统提示或工作区说明文件 |
| `tags` | 能力标签，例如 `browser`、`python`、`windows`，用于按标签分配 |
| `model` | 引擎内使用的模型（可空，空表示引擎默认） |
| `permission_profile` | 沿用 E4 的档位：`read_only` / `workspace_write` / `full`（命名以现有实现为准） |
| `skills` | 绑定的 skill 名称列表 |
| `mcp_servers` | 绑定的 MCP 服务器名称列表 |
| `requires_approval` | 领卡前是否需要人工批准 |
| `concurrency` | 本 Bot 同时执行的卡片上限，默认 1，可调大；每个并行 Attempt 使用独立工作区与会话 |
| `config_version` | 配置每次修改加 1，Runner 据此重新落地配置 |
| `enabled, created_at, updated_at` | |

约束：`(node_id, engine)` 不唯一，一个节点上可以有多个同引擎 Bot（角色不同）。并发同时受两层限制：节点上运行中的 Attempt 数 ≤ `nodes.slots`，单个 Bot 运行中的 Attempt 数 ≤ `bots.concurrency`，两者默认都是 1。

### 6.3 `cards`（新表）

| 字段 | 说明 |
|---|---|
| `id, conversation_id` | 所属 Goal |
| `parent_card_id` | 子卡 |
| `title, goal, acceptance` | 标题、要做什么、验收标准 |
| `assignee_bot_id` | 指定 Bot；为空时按 `required_tags` 进公共池 |
| `required_tags` | 公共池匹配用 |
| `depends_on` | 依赖卡片 ID 列表；依赖全部 `DONE` 才可领取 |
| `inputs` | 输入产物 ID 列表（含依赖卡片产物的引用） |
| `status` | 见 §7.1 |
| `priority` | 整数，越大越先 |
| `review` | `none`（默认，提交即 `DONE`）/ `llm`（提交后进入 `REVIEW`，由编排 LLM 按 `acceptance` 验收） |
| `created_by` | `user` / `kernel` / `bot:<id>`（提议转正后） |
| `current_job_id` | 当前 Attempt（`remote_jobs.id`） |
| `result` | `{summary, outcome, artifacts[]}`，由 Bot 提交 |
| `budget` | `{max_attempts, max_minutes}` 硬上限 |
| `version` | 乐观锁 |
| `created_at, updated_at` | |

### 6.4 `card_events`（新表）

| 字段 | 说明 |
|---|---|
| `id, card_id, seq` | `(card_id, seq)` 唯一，单调递增 |
| `kind` | `comment` / `status` / `progress` / `artifact` / `proposal` / `question` / `answer` |
| `actor` | `user` / `kernel` / `bot:<id>` |
| `mentions` | 被 `@` 的 Bot 或 `kernel` / `user` |
| `body` | 文本，长度上限同 `tool_result_chars` |
| `data` | 结构化附加信息（提议的子卡、状态前后值等） |
| `idempotency_key` | 唯一，防止重复写 |
| `created_at` | |

### 6.5 `remote_jobs`（复用为 Attempt，加列）

加 `card_id`、`bot_id`；`parent_task_id` 允许为空（已是可空）。`engine`、`config_snapshot`、`session_id`、`result`、租约、事件序号全部沿用。新链路下一张卡的每次执行就是一条 `remote_jobs`。

### 6.6 `pairing_codes`（新表）

`code_hash, expires_at, used_at, node_name_hint`；一次性，默认 10 分钟有效。

### 6.7 其它

- `memories`（复用，加列）：`kind`（`card` / `goal`）、`ref_id`、`bot_id`、`tags`，见 §9.5。
- `orchestrator_runs`（新表）：每轮编排的触发事件、工具调用、输出、耗时、token 用量，见 §9.1、§9.6。

## 7. 白板协议

### 7.1 卡片状态机

```
                                                       ┌──(card_submit, review=none)───────────────────▶ DONE
DRAFT ──(编排/用户确认)──▶ READY ──(Runner 领取)──▶ RUNNING ──(card_submit, review=llm)──▶ REVIEW ──(验收通过)──▶ DONE
  │                        ▲  │                       │                        │
  │                        │  │(需审批)               ├─(card_block)──▶ BLOCKED ─┤(用户/编排回复后)──▶ READY
  │                        │  ▼                       │                        └─(验收不通过)──▶ READY（附评论）
  │                        └─ WAITING_APPROVAL         ├─(失败/超时，未超预算)──▶ READY
  │                                                    └─(超预算)──▶ FAILED
  └──────────────────────────────(任意时刻用户取消)───────────────────▶ CANCELLED
```

- `READY` 才能被领取；领取条件：依赖全 `DONE`；`assignee_bot_id` 匹配，或公共池里 `required_tags ⊆ bot.tags`；Bot 启用；节点运行数 < `slots` 且该 Bot 运行数 < `concurrency`。
- `WAITING_APPROVAL`：Bot 设置了 `requires_approval`，或卡片涉及外部动作（编排 LLM 标记），需要用户在 Desktop / 手机上批准后才进入 `READY`。
- 验收：**默认不验收**（`review=none`），`card_submit` 后卡片直接进入 `DONE`。用户或编排 LLM 建卡时可以指定 `review=llm`，此时卡片先进入 `REVIEW`，由编排 LLM 按 `acceptance` 验收。无论哪种方式，用户都可以在 `DONE` 卡片上点"重开"，附评论后卡片回到 `READY`。
- `BLOCKED`：Bot 需要人或其它卡片的输入。被回复后回到 `READY`，Runner 用会话续接（沿用 E5）继续执行。

### 7.2 领取与租约

沿用 `remote.py` 现有机制，只是查询目标从"远程工作包"改成"卡片"：

1. Runner `POST /api/runner/{node_id}/claim`，带上空闲的 Bot 列表。
2. Kernel 在一个事务内用 `FOR UPDATE SKIP LOCKED` 选出可领卡片，创建 `remote_jobs`（`attempt_id`、`lease_until`），把卡片设为 `RUNNING`。
3. 返回 Attempt：卡片内容、输入产物、Bot 配置快照（含 `config_version`）、会话 ID（续接时）、**卡片级白板令牌**（§8.3）。
4. Runner 定期续租；租约过期由 Kernel 把卡片放回 `READY`（计入 `max_attempts`）。
5. 过期 Attempt 的任何写入一律拒绝（沿用 run token 校验）。

Claim 改为**长轮询**（最长 25 秒挂起），有新卡、评论 `@Bot` 或取消时立即返回，避免频繁空轮询。

### 7.3 沟通：评论与 `@`

- Bot 在卡片下写评论，`@kernel` 表示请编排 LLM 处理，`@user` 表示请用户回答，`@另一个Bot` 表示知会对方。
- **知会不是派活**：`@另一个Bot` 只把评论推到对方的收件箱（§8 `inbox`），对方在执行自己的卡时可以读到；如果要对方做事，必须写 `proposal`，由编排 LLM 决定建卡。
- 投递时机（引擎不支持在运行中注入消息时）：
  - 对方正在运行：评论进收件箱，对方调用 `inbox` 时读到。Pi 支持 steering 时可以即时注入。
  - 对方卡片处于 `BLOCKED` / `REVIEW` 且被回复：卡片回到 `READY`，下次领取时续接会话，并把新评论放在提示开头。
- 防失控：每张卡每小时评论数上限、每个 Goal 的提议数上限、全局"停止全部 Bot"开关（沿用 `bot_control`）。

### 7.4 提议子任务

Bot 调用 `card_propose`，在当前卡片下写一条 `proposal` 事件（包含 title / goal / acceptance / 建议 Bot 或标签）。编排 LLM 被唤醒后决定：转成 `DRAFT` / `READY` 子卡、合并到已有卡，或拒绝并附理由。Bot **不能**直接创建 `READY` 卡。

### 7.5 并发写

所有对卡片的修改都带 `expected_version`（与现有 `board.py` 一致），冲突返回 409，由调用方重读后重试。评论只追加，不需要版本号。

## 8. 白板工具（Bot 侧）

### 8.1 工具清单

| 工具 | 作用 | 约束 |
|---|---|---|
| `board_view` | 读当前 Goal 的卡片列表（标题、状态、负责人、结果摘要） | 只读本 Goal |
| `card_get` | 读一张卡片的详情与最近 N 条事件 | 只读本 Goal |
| `inbox` | 读发给本 Bot 的新评论，读后标记已读 | |
| `card_progress` | 写进度（一句话 + 可选百分比） | 仅本卡，限频 |
| `card_comment` | 在本 Goal 任意卡片下评论，可 `@` | 限频 |
| `card_propose` | 提议子卡 | 仅本卡下 |
| `card_block` | 声明阻塞并提问（`@user` / `@kernel`） | 仅本卡；Attempt 随即结束，会话保留 |
| `card_submit` | 提交结果摘要与产物，进入 `REVIEW` | 仅本卡；Attempt 结束 |
| `artifact_put` | 上传工作区文件为产物 | 沿用分片上传 |
| `artifact_get` | 下载输入产物到工作区 | 仅本卡 `inputs` 及本 Goal 已完成卡片的产物 |

### 8.2 三种引擎如何拿到这些工具

后端实现只有一份：**Runner 内置的本地白板代理**，把调用转发到 Kernel 的 `/api/runner/.../board/*`。暴露方式按引擎区分：

| 引擎 | 暴露方式 | 备注 |
|---|---|---|
| Claude Code | stdio MCP Server（`niucai-board-mcp`，Runner 自带的小程序），通过 MCP 配置文件传入 | 现有 Runner 用 `--bare`，不会自动发现 MCP，需要显式传 MCP 配置；具体参数以锁定版本为准 |
| Codex | 同一个 stdio MCP Server，写入该 Bot 独立 HOME 下的 Codex 配置 `mcp_servers` 段 | 配置格式以锁定版本为准 |
| Pi | 不接 MCP（Runner 以 `--no-mcp` 启动），改用 Pi 扩展注册同名自定义工具，直接调用 Runner 本地代理 | 已实现：`services/node-runner/board-extension.ts` |

这样三种引擎看到的工具名和语义完全一致，编排 LLM 和用户也不用关心卡片由哪种引擎执行。

### 8.3 白板令牌

claim 返回一个**卡片级令牌**（随 Attempt 失效）。Runner 只把它交给本地白板代理，不写进 argv 或环境变量以外的位置，也不交给引擎进程。代理凭这个令牌调用 Kernel；Kernel 按令牌限定 Goal、卡片和 Bot 身份。节点令牌（长期）只用于心跳和领取。

## 9. Kernel 编排 LLM

### 9.1 运行方式

- **不使用 Pi**：直接用 `models/gateway.py` 做 tool-calling 循环，模型角色用 `planner`。
- 由 Worker 事件驱动，每个 Goal 同一时间最多一轮编排（行锁）；每轮有工具调用次数上限和 token 上限。
- 触发条件：用户在 Goal 中发消息；卡片进入 `REVIEW`（仅 `review=llm`）/ `BLOCKED` / `FAILED`；卡片 `DONE` 后解锁了依赖它的卡片或需要汇总；收到 `proposal`；评论 `@kernel`；Goal 下所有卡片都进入终态。
- 每轮编排记录为一条 `orchestrator_runs`（输入事件、工具调用、输出、耗时），用于回放和排障。

### 9.2 编排工具

| 工具 | 说明 |
|---|---|
| `bots_list` | 可用 Bot：名称、角色、标签、引擎、节点在线状态、当前是否忙 |
| `card_create` | 建卡（`DRAFT` 或 `READY`），可指定 Bot 或标签、依赖、输入 |
| `card_update` | 改标题 / 目标 / 验收 / 负责人 / 优先级（带版本号） |
| `card_review` | 验收 `review=llm` 的卡片：`accept` → `DONE`，`reject` → `READY` 并附评论 |
| `memory_search` | 按关键词检索历史 Goal / 卡片的结论（§9.5） |
| `card_cancel` | 取消 |
| `proposal_decide` | 接受 / 合并 / 拒绝 Bot 的提议 |
| `card_comment` | 回复评论、回答 Bot 的问题 |
| `ask_user` | 向用户提问（进入会话，推送到手机） |
| `goal_reply` | 给用户回消息（进展、最终汇总） |
| `artifact_read` | 读产物摘要（文本截断） |

编排 LLM **没有**浏览器、Shell、文件写入等工具。

### 9.3 上下文

每轮编排的上下文由 Kernel 编译：Goal 的用户消息、白板快照（卡片表格 + 结果摘要）、触发事件详情、Bot 名单。不保存长对话历史，避免上下文膨胀；需要细节时用 `card_get` / `artifact_read` 按需读取。

### 9.4 用户直接操作

用户可以绕过编排 LLM：在白板上手动建卡、指定 Bot、拖动状态、验收。这是"快速分配任务"的主要入口，例如"让 Windows 上的 Claude 改这个仓库"可以直接建一张指定该 Bot 的卡。

### 9.5 简单 Memory

目的只有一个：**记住处理过什么任务、结论是什么**，让编排 LLM 下次遇到类似目标时能参考，用户也能翻看。不做向量检索，不做 Bot 私有记忆。

- 复用 `memories` 表，每条记录增加 `kind`（`card` / `goal`）、`ref_id`（卡片或 Goal ID）、`bot_id`、`tags`。
- 写入时机（Kernel 自动写，不经 LLM 判断）：
  - 卡片进入 `DONE`：写一条 `card` 记忆，内容为卡片标题、目标、负责 Bot、`result.summary`、产物名称列表。
  - Goal 全部卡片结束且编排 LLM 给出最终汇总：写一条 `goal` 记忆，内容为用户原始请求和汇总结论。
  - `FAILED` / `CANCELLED` 也写一条，标明失败原因，避免重复踩坑。
- 读取：编排 LLM 用 `memory_search`（关键词 + 标签 + 时间倒序，PostgreSQL 全文检索或 `ILIKE`，第一版取前 10 条）。编排上下文里**不**自动注入全部记忆。
- Bot 侧第一版不提供 Memory 工具；需要历史结论时，由编排 LLM 写进卡片的 `goal` 或 `inputs`。
- 用户可以在 Desktop 的 Memory 页查看、搜索、编辑、删除记忆。

### 9.6 执行历史与进度展示

执行都在 Bot 上，但 Desktop 和手机要能实时看进度、事后回看过程，所以**过程历史必须留在 Kernel**，不能只存在节点上。

| 层 | 内容 | 来源 | 保留 |
|---|---|---|---|
| 卡片时间线 `card_events` | 状态变化、进度、评论、提问、提议、产物 | 白板工具、Kernel 状态机 | 永久（随卡片） |
| Attempt 事件流 `remote_jobs` 事件 | 引擎原始事件经 Runner 映射后的标准事件：思考摘要、工具调用（名称 + 参数摘要 + 结果摘要）、输出片段、错误 | Runner `/events`（沿用现有映射） | 默认永久；可在 Settings 配置原始事件保留天数，过期后只保留 `card_events` 与结果摘要 |
| 结果与产物 | `cards.result`、`artifacts` | `card_submit`、`artifact_put` | 永久 |
| 编排记录 `orchestrator_runs` | 每轮编排的触发事件、工具调用、输出 | Kernel | 永久 |

规则：

- Runner 先把事件写入本地账本再上报（沿用现有 `active.json` 机制），断网期间的事件在重连后按序号补传；Kernel 按 `(job_id, seq)` 去重。
- 单条事件的文本按 `tool_result_chars` 截断，大段输出转成产物，避免数据库膨胀。
- Desktop 实时展示走现有事件 WebSocket：白板卡片显示最新一条进度；打开卡片显示时间线，展开某次 Attempt 显示完整事件流。
- 会话续接（E5）时，同一卡片的多次 Attempt 按时间顺序拼成一条连续的历史。
- 节点上的会话文件和工作区只是缓存：节点被删除或重装后，Desktop 上的历史不受影响，只是该卡片不能再续接会话。

## 10. Runner（节点侧）

### 10.1 协议

把现有 `/api/remote/runner/{node_id}/*` 整理为 `/api/runner/{node_id}/*`，旧路径在过渡期保留为别名：

| 接口 | 说明 |
|---|---|
| `POST /api/pairing/redeem` | 用配对码换节点令牌（不需要 Bearer） |
| `POST /heartbeat` | 上报平台、版本、引擎探测结果、各 Bot 的 `config_version` |
| `GET /config` | 拉取本节点所有 Bot 的配置（角色、skills 内容、MCP 定义、权限档位） |
| `POST /claim` | 长轮询领卡（§7.2） |
| `POST /jobs/{id}/heartbeat`、`/events`、`/finish` | 沿用 |
| `artifacts` / `uploads` | 沿用 |
| `POST /jobs/{id}/board/{tool}` | 白板工具转发（§8） |

### 10.2 配置落地（skills / MCP）

心跳发现某个 Bot 的 `config_version` 变了，Runner 就拉 `/config`，在该 Bot 的独立目录 `<data>/bots/<bot-id>/` 下重新生成：

| 引擎 | instructions | skills | MCP |
|---|---|---|---|
| Claude Code | 工作区说明文件或系统提示参数 | 以 Claude Code skill 目录结构写入（`<name>/SKILL.md`），通过加载目录参数指定 | 生成 MCP 配置文件并显式传入（含白板 MCP） |
| Codex | 工作区 `AGENTS.md` | 若锁定版本支持 skills 目录则写入，否则拼接进 `AGENTS.md` | 写入该 Bot 独立 HOME 的配置 `mcp_servers` |
| Pi | 系统提示 | Pi 的 skills 目录 | Pi 无内置 MCP：本方案只提供白板与浏览器扩展。通用 MCP 由 Pi 桥接扩展实现，**不在 P1–P6 范围内**；在此之前，需要 MCP 的卡片应分配给 Claude / Codex Bot，给 Pi Bot 绑定 MCP 时 Desktop 提示"暂不支持" |

规则：

- Skill 和 MCP 的**定义**由 Kernel 托管（复用 `skills`、`mcp_servers` 表），Bot 只做绑定。同一个 skill 可以绑给多个 Bot。
- MCP 定义里的密钥引用节点本地的密钥名（`env:NAME`），实际值只存在节点上，Kernel 不保存第三方密钥。Desktop 在节点详情页提示"该 MCP 需要在节点上设置 `NAME`"。
- 需要安装的 MCP（`npx` / `uvx` 包），由 Runner 在落地时预热一次并上报结果；失败的 MCP 在 Bot 卡片上显示"不可用"，不影响该 Bot 的其它能力。
- 原先"Kernel 内调用 MCP 经 ActionGateway"的路径随 Kernel 内执行一并下线（§14）。

### 10.3 浏览器

补充（2026-10-29）：已新增可选 Pi 原生浏览器扩展与独立部署模板，工具直接连接与 Runner 共享网络的可见 Chrome loopback CDP；默认只启用浏览器工具，普通 Pi 节点不变。部署及验收边界见 [Pi 浏览器节点与 VNC](pi-browser-node.md)。这不代表通用 MCP 桥接、VPS 部署或节点 HTTPS 预览已验收。

浏览器 Bot 和 Chrome / KasmVNC 部署在同一台 VPS（同一个 compose），Bot 通过 `127.0.0.1:9222` 直连 CDP，不再经过 relay：

- Pi 引擎：把现有浏览器工具迁移为 Pi 扩展。
- Claude Code / Codex：绑定浏览器类 MCP（如 chrome-devtools 或 Playwright 的 MCP，选型待定），配置为连接本机 CDP。

画面预览：KasmVNC 仍在该 VPS 上，**Desktop 直接访问节点，不经 Kernel 代理**。

- 节点用 Caddy 提供 HTTPS（沿用 `deploy/caddy/` 模板），KasmVNC 使用独立的预览口令。
- Runner 在心跳里上报 `preview_url`，Kernel 只保存地址，不保存口令；口令由用户在 Desktop 的节点详情里填写，存在 Desktop 本地凭据存储中。
- 节点没有公网 HTTPS（例如 NAT 后面）时，`preview_url` 为空，Desktop 显示"该节点未提供预览"，不回退到 Kernel 代理。
- `computer_proxy.py`、`computer_preview.py` 和 `relay/` 随之下线（§14）。

### 10.4 Linux Runner

基于现有 `services/node-runner/runner.py` 改造：引擎抽象、独立 UID、进程组清理、账本、产物上传全部保留；新增多 Bot 配置目录、配置落地、白板代理和长轮询领取。

## 11. Windows Runner

目标：一条命令让本机的 Claude Code / Codex 成为 Bot。

| 项 | 方案 |
|---|---|
| 形态 | 同一份 Runner 代码按平台分支，Windows 版打包为单文件 exe，作为 Tauri sidecar 随 Desktop 安装包分发。用户**不需要**另装 Python 或 uv；只需要自己已经装好的 Claude Code / Codex CLI |
| 启动 | **只由 Desktop 拉起**：Settings 里打开"把本机加为节点"，Desktop 完成配对（不需要复制命令）、启动并守护 Runner，异常退出时自动重启 |
| 生命周期 | Desktop 关到托盘时 Runner 继续运行；完全退出 Desktop 时先通知 Runner 停止领卡，并等待当前 Attempt 结束（可选立即取消）。Runner 若被强制结束，租约过期后卡片自动回到 `READY` |
| 升级 | Runner 版本跟随 Desktop 版本；心跳上报 `runner_version`，版本不兼容时 Kernel 拒绝领取并提示升级 Desktop |
| 权限 | 没有 `setuid`，以当前用户身份运行 |
| 进程清理 | 用 Windows Job Object 管理进程树，取消或超时时整树结束 |
| 工作区 | 默认 `%LOCALAPPDATA%\niucai-runner\work\<job>`；卡片可以指定"本机目录"（如某个仓库），但目录必须在节点本地白名单内 |
| 凭据 | 默认复用用户已登录的 Claude Code / Codex 账号；也可以为 Bot 配置独立配置目录 |
| 安全边界 | **只有**工作目录白名单 + 引擎自身的权限模式 + Kernel 侧开工审批。这比 Linux 的独立 UID 弱，Desktop 在添加 Windows Bot 时必须明确提示 |

默认策略：Windows Bot 的 `requires_approval=true`、`permission_profile` 默认最低档；用户可以按 Bot 放开。

## 12. 设备接入与 Desktop 界面

### 12.1 Settings 结构

左下角只保留一个状态点（Kernel 已连接 / 未连接 / 待同步），点击进入 Settings。`ConnectionSwitcher`、"本机控制中心"、"执行资源"、"远程 Pi"页面都并入 Settings：

```
Settings
├─ Kernel        当前连接（地址、状态、版本），切换 / 新增 Kernel 连接，本机受管 Kernel 启停
├─ 节点与 Bot    拓扑图 + 节点列表；每个节点下列出 Bot
│   ├─ [＋ 添加节点]  选择 VPS / Docker / 本机 Windows → 生成配对码与安装命令
│   └─ [＋ 添加 Bot]  选节点 → 选引擎（只列出该节点探测可用的）→ 名称、角色、标签、权限档位
├─ Skills 与 MCP 托管库（原"扩展"页）；在 Bot 详情里勾选绑定
└─ 模型          编排 LLM 使用的模型与 Key
```

### 12.2 添加节点

1. 用户点"＋ 添加节点"，选类型，Kernel 生成配对码（一次性，10 分钟）。
2. 界面显示一条命令，例如 Linux 的 `docker run ... niucai-runner pair --kernel https://k.example --code XXXX-XXXX`；Windows 为 PowerShell 命令；"本机"类型由 Desktop 直接完成，不需要复制命令。
3. Runner 用配对码换取节点令牌并保存在节点本地，开始心跳。界面上的节点从"等待配对"变成"在线"，并列出探测到的引擎。
4. 配对码和令牌都不放进 URL 查询参数。

### 12.3 拓扑图

`GET /api/topology` 返回 `{kernel, nodes:[{id, name, platform, status, bots:[{id, name, engine, status, busy_card}]}]}`。Desktop 用 SVG 画一个简单的放射图：

```
            [Windows 本机]
            ├ Claude·本地编码  ● 忙：修复登录页
            └ Codex·本地编码2  ○ 空闲
                    │
[VPS-A] ──────── (Kernel) ──────── [VPS-B]
 └ Pi·浏览员 ●                      └ Codex·后端 ✕ 离线
```

颜色表示 在线 / 忙 / 离线 / 凭据不可用；点击节点或 Bot 打开详情抽屉。不需要拖拽布局。

### 12.4 主导航

简化为：**对话**（与编排 LLM 的 Goal 列表）、**白板**（当前 Goal 的卡片列，复用并改造现有 Kanban 页；点开卡片显示 §9.6 的时间线与事件流）、**文件**（产物）、**Memory**（§9.5 的任务结论记录）、**Settings**。Bots 的管理进 Settings。

没有任何可用 Bot 时，对话页和白板页顶部如实显示"无可用 Bot"并给出"去添加节点"入口；编排 LLM 仍可建卡，但卡片停在 `READY` 等待领取，Kernel 不提供内置的默认 Bot。

### 12.5 手机 PWA

只需三件事：看白板、审批 `WAITING_APPROVAL` 卡片、回答 `@user` 的问题。

## 13. 安全与审批

| 层 | 机制 |
|---|---|
| 连接 | Desktop→Kernel 用 Bearer Token；节点→Kernel 用节点令牌（配对获得，可吊销、可轮换，沿用 `token_hash`） |
| 卡片 | 白板令牌只在 Attempt 期间有效、只对本卡片和本 Goal 有效 |
| 开工审批 | `requires_approval` 的 Bot 或编排 LLM 标记为外部动作的卡片，需要人工批准 |
| 执行 | Linux：独立 UID + 工作区 + 引擎权限模式；Windows：工作目录白名单 + 引擎权限模式（较弱，见 §11） |
| 审计 | Attempt 的事件流、白板事件、产物都留在 Kernel；Bot 内部工具调用只能按引擎的事件流事后审计，**不能**事前逐条拦截 |
| 急停 | 全局"停止全部 Bot"：Kernel 拒绝新领取，并在下次心跳要求 Runner 取消所有 Attempt |
| 密钥 | 第三方服务密钥只存在节点上；Kernel 不保存 |

需要说明的取舍：旧的 Action Gateway"先审批再执行"只能覆盖 Kernel 自己执行的动作。动作移到 Bot 后，逐条审批变成"按卡片审批 + 权限档位 + 事后审计"。需要逐条把关的操作（例如付款、发消息），应该放在**不给该权限**的 Bot 之外，由用户自己完成，或者让 Bot `card_block` 后请用户处理。

## 14. 保留、改造与下线

| 模块 | 处理 |
|---|---|
| `storage/db.py`、Alembic、认证、事件 WebSocket、产物存储 | 保留 |
| `models/gateway.py` | 保留，作为编排 LLM 的调用层 |
| `control/remote.py`、`api/remote.py` | 改造：领取对象改为卡片，加长轮询、配置下发、白板转发 |
| `services/node-runner` | 改造：多 Bot、配置落地、白板代理、Windows 分支 |
| `control/board.py`、`control/kanban.py`、Desktop `Kanban.tsx` | 改造为白板 |
| `control/skills.py`、`control/mcp.py` | 保留定义管理；去掉 Kernel 内调用 MCP 的路径 |
| `control/bots.py` | 改造为新 `bots` 表的管理 |
| `agents/pi_adapter.py`、`deepagent_adapter.py`、`checkpointer.py`、`services/pi-runtime`（Kernel 侧用法） | 下线；浏览器等工具迁移为节点侧 Pi 扩展 |
| `actions/adapters.py`、`actions/gateway.py` 的浏览器 / Shell 执行 | 下线；审批记录部分并入卡片审批 |
| `control/collaboration.py`、`groups.py`、`handoffs` / `workflows` / `work_items` / `conversation_participants` | 下线 |
| `control/routines.py` | 保留思路，改为"定时创建卡片或 Goal"，到 P3 之后再接 |
| `control/memory.py` | 改造为 §9.5 的简单 Memory（自动写入 + 关键词检索） |
| `computer_proxy.py`、`computer_preview.py`、`relay/` | 下线；预览改为 Desktop 直连节点（§10.3） |
| Desktop `RemotePi.tsx`、`Bots.tsx`、`ResourceManager.tsx`、`KernelManager.tsx`、`Connections.tsx` | 并入 Settings 新结构 |

下线方式：先停止新链路对它们的调用，加开关保持旧链路可用；P6 验收通过后再删代码、出迁移删表。

## 15. 分阶段实施

每个阶段单独提交并通过 `pytest`、`ruff`、`alembic check`、Desktop `npm test`、`format:check`。

### P0 冻结与基线

- 提交或另存当前未提交的 Kanban 改动。
- 记录旧链路的回归测试集合，作为下线前的对照。

### P1 设备接入与 Settings

- `pairing_codes` 表、配对接口、`nodes.platform` 等列（迁移）。
- `GET /api/topology`。
- Desktop：Settings 新结构、添加节点向导、拓扑图；左下角收为状态点。
- 验收：在一台新 VPS 上用一条命令完成配对，拓扑图显示在线和引擎探测结果；吊销后显示离线。
- 实现状态（2026-10-29）：迁移 `c5e21029d009`、`control/pairing.py`、`api/nodes.py`、Runner 自动配对与心跳字段、Desktop `NodesPanel`（Settings“节点与 Bot”页）已完成并有自动化测试；新 VPS 实机配对验收待完成。槽位可配置但调度仍按单任务占用，多槽位领取随 P2。旧“远程 Pi”页的 ResourceManager 暂保留。接口见 [API](api.md#节点配对与拓扑重构-p1)。

### P2 Bot 模型与白板

- `bots`、`cards`、`card_events` 表，`remote_jobs` 加 `card_id` / `bot_id`。
- 卡片状态机、领取（长轮询）、租约、验收接口；Desktop 白板页可手动建卡、指定 Bot、验收。
- Runner：多 Bot 配置目录，按卡片执行，结果写回卡片。
- 白板工具：Claude / Codex 用 MCP，Pi 用扩展。
- 节点 `slots`、Bot `concurrency` 两层并发限制；卡片 `review` 字段（默认 `none`）。
- 执行历史（§9.6）：卡片时间线、Attempt 事件流补传与去重、Desktop 卡片详情页。
- 验收：用户手动建两张卡分给 VPS 上的 Codex Bot 和 Pi Bot，第二张依赖第一张的产物；两者都通过白板工具汇报，进度实时出现在 Desktop 和手机上；Runner 断网 1 分钟后恢复，事件完整补齐且不重复；把节点 `slots` 调为 2 后两张无依赖的卡并行执行。
- 实现状态（2026-10-29）：迁移 `d6e21029d010`（`bots`、`cards`、`card_events`，`remote_jobs` 加 `card_id` / `bot_id` / `board_token_hash`）、`control/cards.py`、`api/board.py`、长轮询领取与两层并发、LOST 宽限期续接已完成并有测试（`tests/test_cards.py`）。Runner（`p2`）支持多作业并发（每作业一个台账 `control/active/<id>.json`）、事件发件箱断网重试、卡片工作区与依赖输入、Bot 模型覆盖，并以 `board_mcp.py`（标准库 stdio MCP 服务器）把白板工具接给 Claude Code / Codex：引擎经每作业的 Unix socket 调 Runner 代理，节点令牌与板令牌都不进入引擎进程。Desktop 新增“白板”页（建卡、指定 Bot、详情时间线与 Attempt 事件、批准/回答/验收/退回/重开/评论）和节点下的 Bot 增删改，拓扑图显示 Bot。接口见 [API](api.md#bot-与白板重构-p2)。评审修复（2026-10-29）：Attempt 事件先写入台账旁的事件日志并记录确认游标，Runner 崩溃或长时间断网后交 finish 前补齐（§9.6）；`budget.max_attempts` 在领取入口统一检查（验收退回、阻塞续接都计入），`max_minutes` 下发为 Attempt 超时并由 Kernel 心跳兜底停止；`inbox` 限定在同一 Goal 内。
- 与方案的差异 / 延后：
  - 产物不走 `artifact_put` / `artifact_get` 工具：依赖卡片的文件随领取内联下发到 `inputs/<card_id>/`，结果文件仍随 finish 内联回传。
  - Pi 白板扩展（2026-10-29 补齐）：`services/node-runner/board-extension.ts` 为卡片作业注册与 `board_mcp.py` 同名同参的 8 个工具，经同一个每作业代理调 Kernel；Runner 心跳带 `pi-board` 特性，Kernel 据此给 Pi 卡片下发白板工具说明，旧 Runner 仍以最终回答提交。`card_submit` / `card_block` 的工具结果终止本轮，Pi 不再写最终回答。Pi 不续接会话，`card_block` 回答后重新执行并在提示中带上回答。
  - Pi 的 Bot 模型覆盖只在节点 `models.json` 已定义该模型时生效（自定义 base_url 场景）。
  - `skills` / `mcp_servers` 仅保存，下发在 P5（已实现）；`review=llm` 的卡片由 P3 编排 LLM 验收（已实现），编排关闭时停在 `REVIEW` 由用户验收。
  - 手机 PWA 的白板页未做；验收场景中的断网、双槽位并行等实机验证待完成。

### P3 编排 LLM

- `orchestrator_runs`、编排工具、触发器、预算。
- 对话页改为 Goal：用户发一句话，编排 LLM 建卡、分配并汇总；指定 `review=llm` 的卡片由编排 LLM 验收。
- 简单 Memory（§9.5）：自动写入、`memory_search`、Desktop Memory 页。
- 实现状态（2026-10-29）：迁移 `e7a21029d011`（`orchestrator_runs`、`memories` 加 `kind` / `ref_id` 等）、`control/orchestrator.py`（事件驱动触发、同 Goal 运行合并、步数 / 工具调用 / 每小时运行数预算、租约）、`api/goals.py` 与 Desktop“目标”页已完成并有测试（`tests/test_orchestrator.py`）。Goal 是 `mode=goal` 的会话；Memory 在卡片完成与 Goal 汇总时自动写入，关键词检索。需 `NIUCAI_ORCHESTRATOR_ENABLED=true`。接口见 [API](api.md#goal编排-llm-与-memory重构-p3)。评审修复（2026-10-29）：Worker 崩溃后租约过期的运行由领取前的独立扫描回收并重新排队原触发（最多 2 次）；编排的 `card_update` 必须带 `version`。端到端验收（真实模型、浏览器 Bot、手机回答）待完成；手机 PWA 的 Goal 页未做。
- 验收：一个需要"浏览器查资料 → 代码 Bot 写脚本 → 汇总"的目标端到端完成；Bot 提议被接受、拒绝各一次；`@user` 提问在手机上回答后继续执行；一张 `review=llm` 的卡被打回一次后通过；再次提出类似目标时，编排 LLM 能检索到上次的结论。

### P4 Windows Runner

- 平台分支、Job Object、工作目录白名单。
- 打包为单文件 exe 并作为 Tauri sidecar；Desktop"把本机加为节点"一键配对、启动、守护、退出时优雅停止。
- 实现状态（2026-10-29）：Runner 平台分支（`win_job.py` Job Object、`NIUCAI_WORKDIR_ALLOW` 白名单、回环 TCP + 每作业密钥的白板 MCP、stdin `drain` / `stop` 控制）、卡片 `workdir`（迁移 `f8a21029d012`）、`resource_type=windows_local`；`services/node-runner/build-windows.ps1` 用 PyInstaller 打成单文件 exe 放到 `apps/desktop/src-tauri/binaries/`，`npm run desktop:build:runner` 打包为 sidecar；Desktop `src-tauri/src/local_node.rs` 负责启动、守护（2→60 秒退避重启，配对类致命错误不重启）、日志、退出时 drain，Settings“节点与 Bot”页的“本机节点”面板一键配对并提示安全边界；Windows 节点上新建的 Bot 默认“只读 + 需审批”（由 Kernel 在创建时按节点平台设置，不只是前端默认值）；回传文件路径统一为 POSIX 格式。有自动化测试（Runner、Rust、vitest）。待实机验证：PyInstaller 构建、无 Python 机器上的完整流程、Codex Windows 沙箱参数、Claude Code 复用登录（不加 `--bare`）。
- 验收：在一台只装了 Desktop 和 Claude Code / Codex CLI（没有 Python / uv）的 Windows 上，打开开关即完成配对；Claude Code 和 Codex 各完成一张指定本地仓库的卡，`slots=2` 时可同时运行；取消时进程树被完整结束；白名单外的目录被拒绝；强制结束 Runner 后卡片在租约过期后回到 `READY`。

### P5 Skills / MCP 下发与浏览器迁移

- 配置落地（§10.2）、MCP 预热与状态上报、Bot 详情页绑定 skills / MCP。
- 浏览器 Bot 迁移到 VPS 本机 CDP；节点 HTTPS 预览与 `preview_url` 上报。
- 实现状态（2026-10-29）：Kernel 在每次领取时把 Bot 绑定且已启用的 skill / MCP 定义展开进 `config_snapshot`（不按 `config_version` 拉取，解绑后下一次 Attempt 即生效）；`env:NAME` 由 Runner 从节点环境变量解析，缺失则跳过该 MCP 并发出 `notice` 事件；`read` 档位不加载外部 MCP。skill 写到每作业的资产目录（Claude Code skill 目录结构），提示词列出路径，所有引擎都能读取；Claude Code 另以 `--add-dir` 提供。Bot 视图上报 `missing_assets` / `node_env`，Desktop 表单提示，Pi Bot 绑定 MCP 时提示“暂不支持”。与方案的差异 / 延后：不做 `<data>/bots/<bot-id>/` 常驻目录与 `/config` 拉取；MCP 预热与状态上报未做；Pi 浏览器 Bot 已新增可选原生扩展并完成本机 CDP/VNC 验证（见 [Pi 浏览器节点与 VNC](pi-browser-node.md)），节点 HTTPS 预览未做；Codex 的 MCP 密钥经 `-c` 出现在命令行参数中。接口见 [API](api.md#bot-的-skills-与-mcp-下发重构-p5)。
- 验收：给 Claude / Codex Bot 绑定一个 skill 和一个 MCP，下一张卡就能用上；解绑后不可用；浏览器 Bot 不经 relay 完成登录态网页任务；Desktop 直连节点打开预览。

### P6 下线旧链路

- 按 §14 删除代码和表（新迁移），更新 `README.md`、`AGENTS.md`、`docs/architecture.md`、`docs/api.md`、`docs/acceptance.md`，并在被取代的旧文档顶部注明。
- 实现状态（2026-10-29）：按 §14“先停调用、加开关”完成第一步：`NIUCAI_LEGACY_EXECUTION_ENABLED`（默认 `true`）为 `false` 时，对话 Task、多 Bot 工作流、群聊与 Routine 不再接收新的 Kernel 内执行（409），已有任务照常收尾。被取代的旧文档顶部已注明。删除代码与表、更新 `deploy/compose/verify-*.py` 留待 P1–P5 实机验收通过后进行。
- 验收：全部测试通过；部署脚本 `deploy/compose/verify-*.py` 更新后通过。

## 16. 已决定事项（2026-10-29）

| # | 问题 | 决定 | 落在 |
|---|---|---|---|
| 1 | 节点槽位 | 节点 `slots`、Bot `concurrency` 默认都是 1，用户可调大，允许同一节点多个 Bot 或同一 Bot 多张卡并行 | §6.1、§6.2、§7.1 |
| 2 | 验收方式 | 默认不验收（`review=none`，提交即 `DONE`）；可按卡片指定 `review=llm` 由编排 LLM 验收；用户随时可重开 | §6.3、§7.1、§9.2 |
| 3 | 画面预览 | Desktop 直接访问节点 HTTPS，不保留 Kernel 代理；无公网 HTTPS 的节点不提供预览 | §10.3、§14 |
| 4 | Memory | 只记录处理过的任务与结论，Kernel 自动写入、编排 LLM 关键词检索；执行过程历史另行完整留存在 Kernel，供 Desktop 展示进度与回看 | §9.5、§9.6 |
| 5 | Windows Runner | 单文件 exe 作为 Tauri sidecar，只由 Desktop 拉起与守护，用户无需另装 Python / uv | §11、§15 P4 |
| 6 | 过渡期默认 Bot | 不保留 Kernel 内 Pi；没有 Bot 就如实显示"无可用 Bot" | §12.4 |
| 7 | Pi 通用 MCP | 走 Pi 桥接扩展，但不在本轮 P1–P6 范围；期间需要 MCP 的卡片分给 Claude / Codex Bot | §10.2 |

仍需在实现时确认的细节（不阻塞 P1）：

- 浏览器类 MCP 的选型（chrome-devtools 或 Playwright），在 P5 实测后确定。
- Attempt 原始事件的默认保留天数（默认永久，P2 上线后视数据量再定）。
- Claude Code `--bare` 模式下 skills / MCP 的传入参数、Codex skills 目录支持，以锁定版本实测为准。
