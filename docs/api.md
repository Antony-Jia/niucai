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
