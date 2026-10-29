# V1 API

除 `/healthz` 与 API schema 页面外，REST 使用 `Authorization: Bearer <NIUCAI_API_TOKEN>`。
Token 必须至少 32 字符。当前为单用户，不实现多租户。

| Method | Path | 行为 |
|---|---|---|
| POST | /api/tasks | 创建会话及首轮执行 |
| GET | /api/tasks | 列表，limit/offset |
| GET | /api/tasks/{id} | 查询完整持久状态 |
| POST | /api/tasks/{id}/pause | 暂停并撤销 run token |
| POST | /api/tasks/{id}/resume | 恢复 PAUSED / WAITING_HUMAN |
| POST | /api/tasks/{id}/retry | FAILED 重试 |
| POST | /api/tasks/{id}/cancel | 取消 |
| GET | /api/tasks/{id}/actions | 动作列表与结果 |
| PUT | /api/tasks/{id}/computer | 为 PENDING / PAUSED / WAITING_HUMAN 未绑定任务选择 Linux Computer |
| GET | /api/tasks/{id}/artifacts | Artifact 元数据 |
| GET | /api/context/{id} | 当前 Context Package |
| POST | /api/computers | 注册 Computer（初始 OFFLINE） |
| GET | /api/computers | Computer 列表 |
| GET | /api/computers/{id} | 设备与控制权 |
| GET | /api/computers/{id}/preview | 认证只读浏览器 JPEG，JSON data URL 与 captured_at，不改变控制权 |
| POST | /api/computers/{id}/take-control | HUMAN 接管 |
| POST | /api/computers/{id}/hand-back | AGENT 交还 |
| POST | /api/computers/{id}/pause | Computer PAUSED |
| POST | /api/computers/{id}/resume | 从 PAUSED 恢复 AGENT |
| GET | /api/agents | Agent 定义 |
| GET | /api/approvals | 按 status 查询（默认 PENDING） |
| POST | /api/approvals/{id}/approve | 批准，body `{"note":""}` |
| POST | /api/approvals/{id}/deny | 拒绝，body `{"note":""}` |
| POST | /api/actions/{id}/reconcile | 人工确认 UNKNOWN 动作结果 |
| POST | /api/memories | 创建 episodic / semantic Memory |
| GET | /api/memories | Memory 列表 |
| GET | /api/artifacts/{id}/content | 认证 Artifact 下载 |
| GET | /api/artifacts/{id}/preview | 认证 PNG/JPEG 预览（最大 2 MiB） |
| GET | /api/events?after=0 | 持久事件回放 |
| WS | /api/events | 首帧认证、事件流与续传 |

创建任务：

```json
{"title":"浏览示例网站","goal":"打开 https://example.com 并总结页面内容","agent_id":"main","computer_id":"registered-uuid"}
```

纯推理任务可以省略 computer_id；真实 Browser 动作需要注册的 Linux Computer。
缺少电脑的浏览器任务进入 WAITING_HUMAN，checkpoint.reason 为 `computer required`。
绑定接口 body 为 `{"computer_id":"registered-uuid"}`；运行中必须先 pause，绑定后显式 resume，不自动批准动作。
预览接口依赖 local adapter，只展示全局 CDP 浏览器的第一个页面，并非完整桌面视频或多电脑路由；不可用返回 503。
动作只能由持有效租约的 Worker 在内部提出，没有公开的任意动作执行 endpoint。

UNKNOWN 确认：

```json
{"outcome":"SUCCEEDED"}
```

可选结果为 SUCCEEDED / FAILED，确认后任务仍等待人工 `resume`。
不能确定是否已发生副作用时，不要确认成功或自动重试。

WebSocket 在连接后 5 秒内发送：

```json
{"token":"same-api-token","after":123}
```

服务返回 connected，然后按 Event.id 顺序发送。
保存最后处理的 id 并用 after 重连；heartbeat 是传输信号，不是持久 Event。
无效认证关闭 code=1008，Token 不放在 URL。

系统级通知、Username/Password/TOTP 登录、设备 WSS 注册均为后续模块。
Chat 已统一为异步会话消息入口，自动创建或加入当前执行。详见 [统一会话协议](unified-conversations.md)。


## Desktop 会话与文件

- `POST /api/chat`：接受 content、conversation_id、computer_id 和可选 client_id，返回 HTTP 202 及 conversation、messages、task、accepted。只返回已保存用户消息；模型回答、工具、审批与产物通过持久事件和会话时间线读取。
- `GET /api/conversations?limit=100`：最近会话。
- `GET /api/conversations/{id}/messages?limit=200`：最近消息，按时间正序。
- `GET /api/artifacts?limit=200`：最近产物元数据。
- `GET /api/artifacts/{id}`：单个产物元数据。
- `GET /api/artifacts/{id}/content`：受 workspace 边界限制的认证下载。
- `GET /api/artifacts/{id}/preview`：同样校验认证、Artifact 记录及 workspace 路径，返回 `{"image":"data:image/png;base64,..."}`（或 JPEG），响应 `Cache-Control: no-store`。仅允许 PNG/JPEG MIME 与文件签名匹配，最多读取 2 MiB；越界/缺文件 404，不支持或签名不匹配 415，过大 413。预览失败仍可使用下载；旧版 Kernel 无此接口时客户端显示下载提示。
- `GET /api/events/recent?limit=100`：最新事件，ID 降序；Desktop 从首项 ID 启动事件续传。

以上 REST 路由统一要求 Bearer Token。

## 会话设置、依赖流程与扩展

详见 [YOLO、对话模式、依赖流程与技能/MCP](yolo-pipelines-extensions.md)。

- `GET /api/conversations/{id}`：会话及 `settings`（`mode`、`autonomy.actions/remote`）。
- `PATCH /api/conversations/{id}`：`{"settings":{...}}` 局部合并；`NIUCAI_YOLO_ENABLED` 关闭时开启 YOLO 返回 409。
- `POST /api/workflows` 可传 `stages`（1–12 个 `{key, agent_id, goal, depends_on, output_requirements}`），按依赖图创建阶段，不可与 `routes` 同用。
- `GET/POST /api/skills`、`GET/PATCH/DELETE /api/skills/{id}`：全局技能库。
- `GET/POST /api/mcp/servers`、`PATCH/DELETE /api/mcp/servers/{id}`、`POST /api/mcp/servers/{id}/refresh`：MCP 服务器；`env`/`headers` 值以 `***` 返回。
- 新动作类型 `skills.read {name}`（LOW）与 `mcp.call {server, tool, arguments}`（只读且服务器允许时 LOW，否则 HIGH），均由 Kernel 执行。
- 新事件：`conversation.settings_updated`、`approval.auto_approved`、`workflow.pipeline_created`、`work_item.blocked`、`work_item.unblocked`、`skill.updated`、`mcp.refreshed`。

## 任务进度合同（第二阶段 K1/K2）

创建、列表、详情、控制与绑定电脑的 Task 响应均增量返回 `progress`。原有 status、checkpoint 保留。

```json
{
  "progress": {
    "phase": "WAITING_APPROVAL",
    "message": "等待你批准或拒绝动作，继续任务不能代替审批",
    "wait_reason": "APPROVAL_REQUIRED",
    "last_progress_at": "2026-10-07T04:00:00Z",
    "allowed_operations": ["pause", "cancel", "approve", "deny"],
    "action_id": "action-uuid"
  }
}
```

- `phase`：QUEUED、PLANNING、MODEL_REQUEST、PROCESSING、EXECUTING_ACTION、RECOVERING、WAITING_APPROVAL、WAITING_RECONCILIATION、WAITING_COMPUTER、WAITING_HUMAN，以及 PAUSED / COMPLETED / FAILED / CANCELLED。
- `wait_reason`：无阻塞时为 null；否则为 APPROVAL_REQUIRED、ACTION_UNKNOWN、COMPUTER_REQUIRED、COMPUTER_HUMAN_CONTROL、COMPUTER_PAUSED、COMPUTER_BUSY、USER_PAUSED、STEP_BUDGET_EXHAUSTED、MODEL_BUDGET_EXHAUSTED、GRAPH_BUDGET_EXHAUSTED、HUMAN_REQUESTED 或 TASK_FAILED。
- `last_progress_at`：UTC ISO 8601，记录持久业务事件，不随任务心跳或客户端轮询变化。旧记录没有进度日志时回退到创建时间。
- `allowed_operations`：pause / resume / retry / cancel 使用现有任务控制接口；attach_computer、approve / deny、reconcile 使用各自独立接口。不存在重复“开始”接口；PENDING 自动领取。
- `action_id`：当前等待审批、待核对或执行中的动作 ID；否则为 null。可能存在多个待处理动作，详情以 actions / approvals 列表为准。

服务端基于任务、动作日志和 Computer 控制权重新校验。待审批、UNKNOWN、未绑定所需电脑或未交还控制权时，普通 resume 返回 409；不能把继续当作批准或核对。FAILED 重试也检查未解决阻塞。
FAILED 若仍有待审批动作，retry 只恢复为 WAITING_HUMAN，必须显式审批后才会排队；仍有 UNKNOWN 时 retry 返回 409。批准一个动作也不会绕过同任务其他未决审批或 UNKNOWN。
任务完成/停止后不再返回有效等待提示；旧终态 checkpoint 的 reason/action_id 会在响应中清理，历史事件不删。
失败保留 error/detail 兼容旧客户端，同时提供 error_detail；恢复时清理活动等待字段，保留 proposal、预算和 Pi 会话恢复信息。

任务相关业务事件在同一事务中携带 `data.progress`，与 REST 使用同一投影。新增 task.phase_changed、task.plan_updated、task.step_completed、task.computer_control_changed；租约续期不生成业务进展。
已失效 Worker 的迟到模型事件不携带 progress，也不会覆盖新一轮任务的进度。客户端按事件 ID 去重，并在事件或重连后刷新 REST；队列对电脑占用的依赖也以最新 REST 为准。
旧 Kernel 没有 progress 时，客户端只显示旧状态和兼容提示，不猜测模型执行阶段或进展时间。

## 任务看板

`GET /api/kanban`（只读，需认证）：返回 `tasks`（进行中及近 24 小时结束的任务，含按 `column` 分列、支撑的 `agents` 及其角色、关联的 `remote_jobs`）与 `resources`（远程/资源节点和电脑的在线状态、是否忙碌及正在处理的任务）。Desktop 的“看板”页每 3 秒轮询。

## 节点配对与拓扑（重构 P1）

需 `NIUCAI_REMOTE_PI_ENABLED=true`，否则返回 409。

- `POST /api/pairing`（需认证）：`{name, resource_type}` → `{code, expires_at, name}`。配对码形如 `ABCD-EFGH`，10 分钟有效、仅可兑换一次，Kernel 只存哈希；响应 `Cache-Control: no-store`。
- `POST /api/pairing/redeem`（无 Bearer，配对码即凭据）：`{code, platform, runner_version}` → `{node_id, token, name, kernel_instance_id, protocol_version}`。码不存在、过期或已用统一返回 409。Runner 设置 `NIUCAI_PAIRING_CODE` 且无凭据时自动兑换，并把凭据写入 `/data/control/credentials.json`（0600）；之后重启直接读取，环境变量 `NIUCAI_NODE_ID`/`NIUCAI_NODE_TOKEN` 仍优先。
- `GET /api/topology`（需认证）：`{kernel: {instance_id, remote_enabled}, nodes: [{id, name, resource_type, platform, runner_version, status: ONLINE|OFFLINE|DISABLED, enabled, slots, busy, engines, preview_url, paired_at, heartbeat_at, bots: [{id, name, engine, role, enabled, running, concurrency}]}]}`。
- 节点心跳新增可选字段 `platform`（linux/windows/macos）、`runner_version`、`preview_url`；`preview_url` 须为 HTTPS（回环地址可用 HTTP），不得含凭据、query 或 fragment。Kernel 只记录预览地址，Desktop 直连节点，不经 Kernel 代理。Runner 从 `NIUCAI_PREVIEW_URL` 读取。
- `PATCH /api/remote/nodes/{id}` 新增 `slots`（1–16，默认 1），节点视图的 `capacity` 改为返回该值；P2 起领取按该值并行（见下节）。

## Bot 与白板（重构 P2）

需 `NIUCAI_REMOTE_PI_ENABLED=true`。Bot 是“节点 + 引擎 + 角色配置”，卡片是白板上的工作单元，每次领取生成一个远端作业（Attempt，复用 `remote_jobs`，带 `card_id` / `bot_id`）。

- `GET /api/bots` / `POST /api/bots` / `PATCH /api/bots/{id}` / `DELETE /api/bots/{id}`（需认证）：字段 `name`（1–60，字母数字、`_`、`-`、中文，用于 `@` 提及，全局唯一）、`node_id`、`engine`（`pi`/`codex`/`claude`）、`role`、`instructions`、`tags`、`model`（空为节点默认）、`permission_profile`（`read`/`workspace`/`full`）、`skills`、`mcp_servers`、`requires_approval`、`concurrency`（1–16，默认 1）、`enabled`（仅 PATCH）。创建时省略 `permission_profile` / `requires_approval` 由 Kernel 按节点给默认值：Windows 本机节点（`resource_type=windows_local` 或 Runner 上报 `platform=windows`）为 `read` + 需审批，其它节点为 `workspace` + 免审批；显式传值优先。视图另含 `running`（活动 Attempt 数）、`node_name`、`config_version`（配置字段变更时递增）。已有执行记录或指派卡片的 Bot 不能删除（409），请改为停用；有执行中卡片时不能改 `node_id`。`skills` / `mcp_servers` 自 P5 起随 Attempt 下发（见下文）。
- `GET /api/board[?conversation_id=]`：`{cards, bots: [{id, name, engine, enabled}], statuses}`。卡片视图含 `assignee_name`、`current_job`、`latest`（最近一条进度/状态/提问，执行中时取引擎最新事件）、`summary`。
- `POST /api/cards`：`{title, goal, acceptance, conversation_id?, parent_card_id?, assignee_bot_id?, required_tags, depends_on, priority, review: none|llm, budget: {max_attempts (默认 3), max_minutes?}, draft}`。`max_attempts` 在领取入口检查：失败重试、验收退回、阻塞回答后的续接都计入执行次数，用尽时卡片直接 `FAILED`，`reopen` 清零。`max_minutes` 随快照下发为 `config_snapshot.timeout_seconds`，取代节点默认超时（`NIUCAI_PI_JOB_TIMEOUT`，最长 24 小时）；超过上限 60 秒后 Attempt 心跳返回 `stop=true`。不指定 Bot 时进入公共池，由 `tags` 覆盖 `required_tags` 的 Bot 领取。依赖成环返回 422。
- `GET /api/cards/{id}`：卡片视图 + `result`、`events`（卡片时间线，按 `seq`）、`runs`（全部 Attempt：`id, bot_id, engine, status, node_id, created_at, updated_at, error`）。Attempt 的引擎事件仍用 `GET /api/remote/jobs/{job_id}/events`。
- `PATCH /api/cards/{id}`：需 `expected_version`，仅 `DRAFT/READY/WAITING_APPROVAL/BLOCKED/FAILED` 可改；`assignee_bot_id` 可置 null。版本不符返回 409。
- `POST /api/cards/{id}/{ready|approve|cancel|accept|reject|answer|reopen}`：`{expected_version?, body}`。`reject` 与 `answer` 需要正文；`reject` 后同一 Bot 续接原会话。`POST /api/cards/{id}/comments`：`{body, idempotency_key?}`，正文中的 `@Bot名` 进入该 Bot 的收件箱。
- 状态机：`DRAFT → READY | WAITING_APPROVAL → RUNNING → DONE`（`review=llm` 时先到 `REVIEW`），`RUNNING → BLOCKED`（`card_block`）→ 回答后重新排队；失败计入 `budget.max_attempts`，用尽为 `FAILED`。Bot 提交（或 Pi 最终回答）后，`review=none` 直接 `DONE`；`review=llm` 进入 `REVIEW`，P3 编排 LLM 实现前由用户 `accept` / `reject`。
- 领取：`POST /api/remote/runner/{node_id}/claim?wait=0..25` 长轮询，有可领卡片或作业立即返回。领取受节点 `slots` 与 Bot `concurrency` 两层限制（`RUNNING`/`CANCELLING`/`LOST` 都占位）。卡片作业的响应带 `board_token`（每 Attempt 一个，Kernel 只存哈希）和 `config_snapshot.card_inputs`（依赖卡片的产物，内联 base64，总量 200 KB 上限），工作区为 `card-<id>`，同卡片多次 Attempt 共用。
- 白板工具：`POST /api/remote/runner/{node_id}/jobs/{job_id}/board/{tool}`，节点认证 + `X-Board-Token`，body `{args}`。工具：`board_view`、`card_get`、`inbox`、`card_progress`、`card_comment`、`card_propose`、`card_block`、`card_submit`；写工具带 `idempotency_key` 去重。`inbox` 只返回同一 Goal 内卡片上 `@` 本 Bot 的评论（无 Goal 的卡片只看无 Goal 的卡片）。Attempt 结束后只允许读工具（409）；令牌错误 403，缺失 422；进度、评论、提议有每 Attempt / 卡片的频率上限（409）。
- 断网：Attempt 租约过期后为 `LOST`，卡片在 `NIUCAI_CARD_LOST_GRACE_SECONDS`（默认 300）内保持 `RUNNING`，同一 Attempt 的心跳、事件、白板调用或 finish 回来即恢复，事件按序号去重；超时后卡片重新排队，LOST 作业在 finish 前仍占节点槽位。Runner 把每条事件先追加到台账旁的 `control/active/<job>.events.jsonl` 并记录已确认序号；Runner 崩溃或断网超过等待时间时，交 finish 前按序补交未确认的事件（Kernel 返回 404/409 则停止补交），交付后与台账一起删除。回传文件的 `path` 一律为 POSIX 格式（Windows 上同样是 `a/b`）。

## Goal、编排 LLM 与 Memory（重构 P3）

需 `NIUCAI_ORCHESTRATOR_ENABLED=true`（创建 Goal 与发消息在关闭时返回 409）并重启 Kernel 与 Worker；编排循环跑在 Worker 内，与 Task 执行槽分开。Goal 是 `settings.mode=goal` 的会话，旧的 `/api/conversations/{id}/messages` 对它返回 409。

- `GET /api/goals?limit=`：`[{id, title, status: ACTIVE|DONE, cards: {状态: 数量}, created_at, updated_at}]`。
- `POST /api/goals`：`{title?, message}` → `{id, title, status}`；写入用户消息并排队一次编排运行。
- `GET /api/goals/{id}`：`{id, title, status, orchestrator_enabled, messages, runs（最近 20 次编排运行）, board（该 Goal 的卡片与 Bot）}`。
- `POST /api/goals/{id}/messages`：`{content, client_id?}`，`client_id` 去重；已结束的 Goal 收到消息后重新 `ACTIVE`。`POST /api/goals/{id}/run`：手动再排一次运行。`GET /api/orchestrator/runs/{run_id}`：运行详情（触发器、步骤、工具调用）。
- 触发：用户消息、卡片进入 `REVIEW`/`DONE`/`FAILED`/`BLOCKED`/`CANCELLED`（用户主动取消除外）、Bot 提议子任务、评论中 `@kernel`、Goal 下全部卡片关闭。同一 Goal 未开始的运行合并触发器；预算由 `NIUCAI_ORCHESTRATOR_MAX_STEPS` / `_MAX_TOOL_CALLS` / `_RUNS_PER_HOUR` / `_LEASE_SECONDS` 控制。Worker 崩溃时，领取前的独立扫描会把租约过期的 `RUNNING` 运行判为 `FAILED`（`lease expired`）并把原触发重新排队（触发器 `recovered`），不依赖该 Goal 再有新事件；同一批触发最多自动恢复 2 次。过期的执行者不能再写入已回收的运行。
- 编排工具：`bots_list`、`card_get`、`card_create`、`card_update`（必须带取自 `card_get` 的 `version`，卡片已变则拒绝）、`card_review`（`review=llm` 卡片的通过/打回）、`card_cancel`、`proposal_decide`、`card_comment`、`ask_user`、`goal_reply`、`memory_search`、`artifact_read`。模型走 Model Gateway 的 `planner` 角色。
- Memory：卡片 `DONE` 与 Goal 汇总时按 `(kind, ref_id)` 自动覆盖写入（`kind` 为 `card` / `goal`）。`GET /api/memories/search?q=&kind=&limit=` 按空格分词做关键词（`ILIKE`）检索，最多 5 个词、任一命中；`PATCH /api/memories/{id}` `{content?, tags?}`；`DELETE /api/memories/{id}`。暂无向量检索。

## Windows 本机节点（重构 P4）

- 节点 `resource_type` 新增 `windows_local`（`POST /api/pairing`、`POST /api/remote/nodes` 均可用）。Runner 在 Windows 上上报 `platform=windows`，以当前用户身份运行，无独立 UID；进程树由 Job Object 管理，取消/超时整树结束。
- 卡片 `workdir`（`POST/PATCH /api/cards`，≤500 字符）：指定节点本机目录作为工作目录。必须同时指派 Bot（不进公共池）；Runner 只接受位于 `NIUCAI_WORKDIR_ALLOW`（按 `os.pathsep` 分隔的绝对路径）内且已存在的目录，否则 Attempt 失败。用户目录中的改动留在原地，不回传文件；依赖卡片的输入写在 Runner 工作区 `inputs/`，作为附加目录提供给引擎。
- 白板 MCP 在 Windows 上改为回环 TCP 端口 + 每作业密钥（`NIUCAI_BOARD_SECRET`），打包后的 exe 用 `niucai-runner.exe board-mcp <address>` 子命令提供。
- Desktop sidecar：`NIUCAI_RUNNER_STDIN_CONTROL=1` 时 Runner 从 stdin 读取 `drain`（停止领取、等当前作业结束后退出）和 `stop`（取消当前作业后退出）。Desktop 只在首次启动时传 `NIUCAI_PAIRING_CODE`，凭据保存在 `%LOCALAPPDATA%\niucai-runner\control\`，日志在同目录 `runner.log`（5 MB 轮转一份）。

## Bot 的 skills 与 MCP 下发（重构 P5）

- Bot 的 `skills` / `mcp_servers` 是 Kernel 托管定义（`/api/skills`、`/api/mcp/servers`）的名称列表。每次领取时 Kernel 把**已启用**的定义展开进 `config_snapshot`：`skills: [{name, description, content, version}]`、`mcp_servers: [{name, transport: stdio|http, config: {command, args, env} | {url, headers}}]`。解绑或停用后，下一次 Attempt 即不再带上；Pi Bot 不下发 MCP（创建时已拒绝绑定）。
- MCP 定义里的值可写成 `env:NAME`，由 Runner 从节点环境变量解析，密钥不经过也不保存在 Kernel。缺少变量、名称不合法（`[A-Za-z0-9][A-Za-z0-9_-]{0,63}`，`niucai-board` 保留）或定义不合法的服务器整体跳过，并产生一条 `notice` 事件；`read` 档位不加载任何外部 MCP。
- `GET /api/bots` 的视图新增 `missing_assets`（绑定了但不存在或已停用的名称，形如 `skill:x`、`mcp:y`）与 `node_env`（需在节点上设置的变量名），Desktop Bot 表单据此提示。
- Runner 落地：skill 写到 `<data>/assets/<job>/.claude/skills/<name>/SKILL.md`（无 frontmatter 时补上），提示词列出每个 skill 的名称、描述与路径，作业结束后删除；Claude Code 另以 `--add-dir` 提供该目录。Claude Code 的 MCP 合并进 `--mcp-config` 文件（含外部 MCP 时写成 0600 文件，不进命令行）并加入 `--allowedTools mcp__<name>`；Codex 通过 `-c mcp_servers.<name>.*` 传入（已知限制：解析后的密钥出现在 Codex 的命令行参数中）。
- 未实现：MCP 预热与状态上报、浏览器 Bot 迁移到 VPS 本机 CDP（§10.3）。

## 旧执行链路开关（重构 P6）

`NIUCAI_LEGACY_EXECUTION_ENABLED`（默认 `true`）。设为 `false` 后，Kernel 内执行的入口返回 409（“Kernel 内执行已关闭…请改用 Goal 或白板”）：`POST /api/tasks`、`POST /api/chat`、`POST /api/conversations/{id}/messages`，以及多 Bot 工作流、群聊、Routine 的新建。已在执行或等待收尾的任务照常完成；Goal、白板、节点与 Bot 不受影响。旧代码与表在 P6 实机验收通过后再删除（新迁移）。
