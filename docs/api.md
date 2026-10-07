# V1 API

除 `/healthz` 与 API schema 页面外，REST 使用 `Authorization: Bearer <NIUCAI_API_TOKEN>`。
Token 必须至少 32 字符。当前为单用户，不实现多租户。

| Method | Path | 行为 |
|---|---|---|
| POST | /api/tasks | 创建独立任务 |
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
V1 不把 Chat 消息自动转成 Task。


## Desktop 会话与文件

- `POST /api/chat`：`{"content":"你好","conversation_id":null}`，返回 conversation 和这一轮两条 messages。executor 通过统一 Model Gateway 回复，Chat 无工具、无任务状态副作用。模型失败会保存 FAILED 回复；同会话已有 PENDING 回复时返回 409。
- `GET /api/conversations?limit=100`：最近会话。
- `GET /api/conversations/{id}/messages?limit=200`：最近消息，按时间正序。
- `GET /api/artifacts?limit=200`：最近产物元数据。
- `GET /api/artifacts/{id}`：单个产物元数据。
- `GET /api/artifacts/{id}/content`：受 workspace 边界限制的认证下载。
- `GET /api/events/recent?limit=100`：最新事件，ID 降序；Desktop 从首项 ID 启动事件续传。

以上 REST 路由统一要求 Bearer Token。
