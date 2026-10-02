# Pi Durable 研究与 niucai 集成

研究日期：2026-10-02。实际集成并锁定 `@earendil-works/pi-durable`、`pi-ai`、`chord` 的 **1.0.0**，不是另一个同名 Pi 项目。官方仍将 Durable 标为 Experimental，升级必须同时审阅恢复测试和存储格式，不能使用浮动版本直接更新生产会话。

## 适配判断

Pi Durable 是可嵌入的 TypeScript Agent harness，不是远程电脑、业务任务队列或权限系统。
它把 conversation、submission、模型 generation、工具 task 与应用 document 放进持久 Session；
每次 commit 作为一个存储边界提交，推理循环可以在同一 transcript 中消费工具反馈。
这符合 niucai 的目标：让模型及时纠错，同时让系统保有权威状态与设备执行权。

| 原功能 | 本次实现 | 权威状态归属 |
|---|---|---|
| DeepAgents 推理循环 | Pi Durable generation / tool tasks | Pi 会话 |
| LangGraph 会话检查点 | Pi JSONL Session，fsync | Pi 会话 |
| 子 Agent 图恢复 | task-owned child conversation + stable submission | Pi 会话 |
| Agent context 压缩 | Pi CompactionTask，摘要请求回到 Kernel summarizer | Pi 会话 |
| 初始计划与后续修改 | Planner + update_plan 工具 | Kernel Task.plan |
| 模型供应商与 Key | 私有 stdio provider → Python ModelGateway → OpenRouter | Kernel |
| Task pause/resume/retry/cancel | PostgreSQL Task、TaskRun、租约 | Kernel |
| 工具执行、审批、审计 | execute_action → ActionGateway | Kernel |
| Computer / 人工接管 | Control lease + 每次请求前后检查 | Kernel |
| Context / Memory / Artifacts / Events | 沿用现有服务 | Kernel |

新任务默认 `NIUCAI_RUNTIME=pi`。旧 DeepAgents/LangGraph 检查点只用于既有任务的兼容恢复，
不把它们伪装转换为 Pi Session，也不删除旧表。`structured` 保留为明确选择的简化运行时。

## 关键恢复语义

Pi 的内部 task 与 Kernel Task 是两类对象。Kernel 的任务编号只绑定一个持久 Pi 根会话；
同一 requestId 重新提交会取得原 submission，不能用每次启动的新编号冒充恢复。

工具在执行前已有持久意图。`replay: safe` 允许中断后重新进入工具函数，但并不保证浏览器副作用恰好一次。
本项目的 replay-safe 工具只是 Kernel 桥接：它用 **Kernel task id + Pi tool task id 的哈希** 查持久 Action 日志。
供应商在不同轮次重复 tool call id 时，不会误把两个动作合成一个。

- Action 已 SUCCEEDED / FAILED / DENIED：返回原结果，不重新派发、不重复计数。
- 动作结果已提交，Pi 尚未保存反馈：重新进入工具，消费原日志。
- WAITING_APPROVAL：停止本次进程并保留未完成 Pi 工具；恢复仍检查 Approval，resume 不代表批准。
- EXECUTING 在崩溃后、或可能发生副作用的执行器异常：按 UNKNOWN 等待人工 reconcile。
- 人工等待：记录工具 task id；只有用户显式 resume 才写入回应标记，租约自动恢复不会代替用户回应。
- 已有最终回答，但 Kernel 未完成：读取原 submission 的答案，不再次调用模型。
- Pi submission 已 unanswered：Kernel 标记 FAILED；显式 retry 在同一个 conversation 内提交新的稳定请求。
- 内部模型轮次与外部动作都有 Kernel 预算；耗尽后保留未完成 generation / tool，等待 resume。

**暂停没有使用 `Conversation.abort()`。** Abort 会终结拥有的工作；本实现停止进程保留 pending work，
恢复时重新打开 Session。取消任务后 Kernel 不再调度该 Session，保留日志供审计。

## 存储与单写者限制

官方可用后端为 Memory、SQLite 和 JSONL，没有现成 PostgreSQL storage。
本次选择 JSONL + `fsync: true`，而不是临时 Memory 或默认 SQLite WAL 的弱同步配置。
Kernel PostgreSQL 与 Pi 文件存储之间没有分布式事务；Action 幂等日志覆盖二者之间的恢复间隙。

Session 放在独立 `pi_sessions` 持久卷 `/var/lib/niucai/pi`，不能放进 Agent 可读写 workspace。
每个任务有独立目录和非阻塞 `flock`。Python 子进程继承 lock fd；Python 意外死亡也不会提前释放活着的 Node writer 的锁。
Node 检测到 stdin EOF 会退出；正常清理先停止并等待 Node 退出，再释放锁。

当前部署是**单个 Linux Worker、同一主机持久卷**。不能让不同主机的 Worker 读取各自的本地 Session 副本；
也不能把未经验证的 NFS 锁当成跨主机一致性。未来需要分布式部署时，先实现共享存储和单写者协调。

## 模型与上下文

Node 不读取 OpenRouter Key、不监听 HTTP 端口、不安装 CodingTools，也没有直接电脑/文件/shell 工具。
Pi model id 使用符号角色 `executor`；真实模型、reasoning、max_tokens、费用事件由 Python ModelRouter 决定。
Pi compaction 的 beforeCompact hook 请求 Kernel `summarizer` 角色，必须在 models.yaml 中配置该角色。
`context_window` 默认 64000，应按实际 executor 模型填写；错误窗口会使压缩触发点失真。

每次恢复更新 Kernel context，但保留原 transcript 和工具反馈。压缩后的摘要属于 Agent context，
不是自动写入 Kernel semantic memory。本次没有引入复杂 RAG、自动长期记忆或新的 Memory 数据源。

Gateway 当前是非流式 OpenRouter 请求，Pi provider 在收到完整结果后发出 start/done；
支持用量与费用记录，不宣称已经把逐 token 输出传到 Desktop/PWA。

## 部署与迁移

本地 Worker 需要 **Linux、Python 3.12、uv、Node 24**。Windows Desktop 与 Android PWA 仍是客户端，
无需在手机或 Windows 客户端安装 Pi，也无需更改 REST/WebSocket 协议。

```bash
# 仓库根目录
cd services/pi-runtime
npm ci --ignore-scripts
npm run check
npm run build
npm test
cd ../kernel
uv sync --frozen --extra harness --extra browser
uv run --no-sync alembic upgrade head
uv run --no-sync python -m niucai.worker
```

Docker 从仓库根目录构建：

```bash
docker build -f services/kernel/Dockerfile -t niucai-kernel .
```

镜像含 Node 24、编译后的 Pi bridge 和旧任务兼容依赖。Compose 自动挂载新持久卷。
升级现有部署前：停止旧 worker，备份 PostgreSQL、workspace 与旧会话，更新镜像与 `.env` 的 `NIUCAI_RUNTIME=pi`，
执行迁移并启动一个 worker。已有 LangGraph 检查点自动继续使用 DeepAgents；已有 structured proposal 或无 Pi/图检查点的已执行任务自动使用 structured。
Task.checkpoint.runtime 第一次领取后固定，改变默认配置不会偷偷切换运行中的任务。

新部署还需配置 executor、planner、summarizer 模型与 OpenRouter Key。设备未接入时保持 adapter=disabled。
备份恢复应在停止 Worker 后，同时保存数据库与 pi_sessions；只恢复数据库不能恢复 Pi 推理 Session。
不执行 `docker compose down -v`，否则会删除包括会话在内的持久数据。

## 验证范围

测试用真正的 Pi Durable Node 进程，模型与电脑 I/O 为可控 fake，不只是测试 Python mock adapter。
持续会话测试对 Pi 与旧 DeepAgents 跑相同矩阵：即时纠错、审批批准/拒绝、重启、接管、预算、子 Agent、UNKNOWN、
动作提交与最终回答后的崩溃、重复供应商 call id、只读工具失败、计划修订与人工等待。
另有 Pi 单写者互斥、阻塞模型调用撤销、失败 submission 重试、运行时迁移和自动租约恢复不得回应人工等待的测试。
CI 在 SQLite/PostgreSQL 两个数据库矩阵执行；Node provider 测试检查 transcript、图片、reasoning、usage 和错误协议。

真实 OpenRouter/Chromium/KasmVNC 的设备验收仍需用户后续配置，不以 fake 测试代替。

## 一手资料

- [Pi Durable README](https://github.com/earendil-works/pi/blob/main/packages/durable/README.md)：持久会话、恢复、工具 replay、子 Agent、compaction、storage。
- [Durable specification](https://github.com/earendil-works/pi/blob/main/packages/durable/docs/spec.md)：commit、任务生命周期与恢复语义。
- [官方源码](https://github.com/earendil-works/pi/tree/main/packages/durable/src)：对照 npm 1.0.0 的实际类型与实现，而非猜测 API。
- [官方 pi-ai](https://github.com/earendil-works/pi/tree/main/packages/ai)：custom provider、transcript 与模型事件协议。
