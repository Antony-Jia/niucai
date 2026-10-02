# Kernel 边界与恢复语义

Kernel 拥有 Task、Computer、Control、Approval、Action、Context、Memory、Artifact、Event 与 Model Policy。
Pi Durable 拥有持续推理循环、工具反馈、子 Agent 和会话持久化；
所有真实电脑动作经过 Kernel 包装工具，不能直接调用 Playwright 或 Shell。
完整研究、选型与迁移见 [Pi Durable](pi-durable.md)。

## 一轮任务

1. Worker 用 PostgreSQL `FOR UPDATE SKIP LOCKED` 领取 PENDING 或租约过期的 RUNNING 任务。
2. 生成 run_token、写入 TaskRun、启动心跳；第一次领取时固定运行时。
3. Context Compiler 读取权威状态与相关记录，产生 Context Package。
4. Planner 通过 Model Gateway 产生类型化 Plan；Kernel 验证并持久化。
5. Python Worker 打开任务的单写者锁，启动私有 stdio Node harness；Pi 打开原 JSONL Session。
6. 更新 Kernel context，使用稳定 requestId 提交/恢复同一 submission。
7. Pi 在同一 transcript 中选择工具；execute_action 交回 Kernel 校验类型、控制权、策略与审批。
8. 执行器结果、Artifact、Event、Audit 存入数据库，再把工具反馈返回模型；模型可即时纠错与调整计划。
9. 审批、UNKNOWN、人工等待或预算耗尽时停止该进程，保留 Pi 未完成工作，并把 Task 转为 WAITING_HUMAN。
10. 条件满足后重新领取任务，打开同一 Session；已提交最终答案则直接完成 Task。

Task 状态写入检查 RUNNING、run_token 和 lease_until；模型请求前后与工具派发前都检查租约。
模型只提出动作、计划和答案；Task 的生命周期不会由模型 JSON 任意修改。
每轮最多 max_model_turns 次模型请求，每个任务的动作预算由 max_steps 与 checkpoint.budget_limit 控制。

## 并发和接管

锁顺序为 Task → Computer → Action/Approval。Computer.active_task_id 限制设备的同时使用。
执行器运行时保留 Task/Computer 行锁；take-control、pause、cancel 等待当前有界动作结束后撤销租约。
当前默认动作时限 30 秒、租约 90 秒；接管不承诺撤销已经送出的浏览器点击。
Node 没有直接设备连接；租约监测会取消阻塞的模型请求并停止 Node，迟到的模型结果不能执行。
接管与交还使旧 browser refs 失效，需要重新 snapshot。手动暂停不因 hand-back 自动恢复。

Pi 存储没有内置跨进程互斥。本项目每任务 flock、继承 lock fd、stdin EOF 退出与先停进程再解锁保证单写者。
本版使用单个 Linux Worker 和同一主机上的独立持久卷，不能直接扩成多主机本地存储部署。

## 崩溃和幂等

Pi JSONL 与 Kernel PostgreSQL、真实浏览器之间都不存在原子事务，不承诺外部动作 exactly-once。

- Pi commit 使用 fsync；会话和未完成工具意图保存在 workspace 之外的 pi_sessions 卷。
- replay-safe 桥接工具使用 task_id:pi:hash(Pi tool task id) 的幂等键，恢复先读 Action 日志。
- Action 终态返回原结果，已 counted_actions 的动作不重复计数。
- 执行前持久化 EXECUTING；执行中崩溃或不确定的副作用异常恢复为 UNKNOWN。
- 人工 reconcile 明确 SUCCEEDED/FAILED 后，恢复消费原日志，不再次派发。
- Pi 已保存最终回答但 Kernel 尚未完成：恢复消费原答案，不再次请求模型。
- wait_for_human 只有显式 resume 才写入回应标记；自动租约恢复不能代替用户回应。
- 已终结的 unanswered submission 可显式 retry，在同一 conversation 内提交新的稳定请求。

## 审批与反馈

browser.click、browser.fill、files.write、shell.exec 默认 HIGH，逐动作审批。
其它已注册动作 LOW。Pydantic 再次校验动作，禁止未知类型/多余字段；Shell 默认关闭。
Approval 绑定不可变 spec；审批不代表设备控制授权，执行前再次检查租约及 HUMAN 状态。
resume 不能绕过待审批动作或 UNKNOWN。拒绝审批返回 DENIED，模型可选择其它办法。
只读 browser.snapshot/files.read 的超时与错误返回 FAILED，允许即时重试；可能发生副作用的异常保守按 UNKNOWN。

## Model、Context 与子 Agent

Role → ModelRouter → ModelGateway → OpenRouter。Provider Key 留在 Python，Node 只知道符号 executor 角色。
初始 Planner 计划与 update_plan 的修订存到 Kernel；Pi transcript 直接消费后续工具反馈。
Pi 自动 compaction 通过 Kernel summarizer 生成摘要；压缩不自动写入 Kernel 长期 Memory。
每次领取/恢复重新编译当前 Kernel context，原 Pi 会话继续保留。

`task` 创建 task-owned child conversation，复用 Kernel 工具与模型角色，以稳定 requestId 恢复子任务。
子 Agent 不注册进一步递归委派工具，限制第一版复杂度。未安装任何直接操作电脑的 CodingTools。
Gateway 目前返回完整模型结果，尚未实现 Desktop/PWA 的逐 token 输出。

## 迁移与事件

新任务默认 Pi；已有 harness_checkpoints 自动绑定 DeepAgents，已有 structured proposal 或无会话检查点的已执行任务自动绑定 structured。
Task.checkpoint.runtime 固定后不随默认配置改变；保留旧依赖与表只用于原任务兼容。
升级先停止 Worker、备份数据、迁移数据库，再启动一个新 Worker；无需改 Desktop/PWA 协议。
数据库和 Pi Session 必须一起备份恢复，单独恢复数据库无法还原 Pi transcript。

Event 与 Kernel 状态在同一事务写入。PostgreSQL 事务级 advisory lock 串行化 Event.id 分配，
客户端按 after 游标重连并去重。Pi Session 是单独存储，agent.started 事件只标识本次接入，不宣称跨存储事务。
OpenTelemetry span 和模型/动作事件沿用现有实现；默认不向外部遥测服务发送。
