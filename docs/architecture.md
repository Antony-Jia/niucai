# Kernel 边界与恢复语义

Kernel 拥有 Task、Computer、Control、Approval、Action、Context、Memory、Artifact、Event 与 Model Policy。
Agent Runtime 只返回 Plan / Decision；无法直接调用 Playwright 或 Shell。

## 一轮任务

1. Worker 用 PostgreSQL `FOR UPDATE SKIP LOCKED` 领取 PENDING 或租约过期的 RUNNING 任务。
2. 为该轮生成 `run_token`，写入 TaskRun；独立心跳延长租约。
3. Context Compiler 读取权威状态与相关记录，产生 Context Package。
4. Planner 通过 Model Gateway 产生类型化 Plan；Kernel 验证并持久化。
5. Executor / DeepAgents 产生 Decision；Kernel 先存 proposal，再进入 Action Gateway。
6. Action Gateway 校验类型、控制权与策略；高风险动作创建 Approval。
7. 执行器运行动作；动作结果、Artifact、Event、Audit 存入数据库。
8. Kernel 更新 checkpoint/current_step，再调用下一轮决策或结束任务。

所有 Task 状态写入都检查 RUNNING、run_token 与 lease_until。
旧 Worker 即使收到迟到的模型响应，也不能推进状态或开始新动作。

## 并发和接管

锁顺序统一为 Task -> Computer -> Action / Approval。Computer 当前 active_task_id 保证单个设备仅一个任务执行。
执行器运行时保留 Task 和 Computer 行锁；因此 take-control / pause / cancel 等待当前有界动作结束，
然后撤销 Worker 的 run_token。此后旧 Worker 的结果不能推进任务。

当前设备动作时限默认 30 秒；租约 90 秒。不能承诺瞬时停止已经发送给浏览器的点击。
接管和交还都会使旧浏览器 refs 失效；Agent 需重新 snapshot。
手工暂停的任务不因 hand-back 自动恢复，仅设备接管引起的暂停会恢复。

## 崩溃和幂等

数据库和真实浏览器之间不存在原子事务，不承诺外部动作 exactly-once。

- proposal 写入后崩溃：恢复使用同一 proposal。
- Action 由 `task_id:current_step` 幂等键定位，避免重复创建。
- 已成功/失败/拒绝的 Action：恢复消费原结果，不重新执行。
- 执行前先持久化 EXECUTING；进程在执行中崩溃，恢复将其标记 UNKNOWN。
- 超时或执行器可能已发生副作用的异常同样 UNKNOWN，任务 WAITING_HUMAN。
- 用户检查设备后调用 reconcile，明确 SUCCEEDED / FAILED，然后 resume。
- 外部副作用完成而数据库提交失败：已有 EXECUTING 记录，按 UNKNOWN 处理。

## 审批策略

browser.click、browser.fill、files.write、shell.exec 默认 HIGH，逐动作审批。
其它已注册动作 LOW；禁止未知类型/多余字段。Shell 还需要部署级显式启用。
审批针对持久化的不可变 spec，决策只能执行一次，已取消任务不能审批。
审批不代表 Computer 控制授权：执行时再次检查租约和 HUMAN 状态。

## 事件

事件与对应状态写入同一事务。PostgreSQL 通过事务级 advisory lock 串行化事件 id 分配，
避免游标越过较早分配、较晚提交的 Event。客户端用 after 游标重连，消费时按 Event.id 去重。
WebSocket 是数据库持久事件日志的传输层，断线不会丢 Task 状态。

## Model / Harness

Role -> ModelRouter -> ModelGateway -> OpenRouter。
DeepAgents 自己的工具只操作默认虚拟 Backend；未接入宿主文件系统或真实电脑工具。
每轮 DeepAgents / LangGraph 执行由 Kernel 触发，权威恢复点在 Kernel，
不是 DeepAgents 本地内存。需要更复杂 Harness 内部中断时，可另加持久 Checkpointer，
仍保留 Kernel 的状态和 Action Gateway 边界。

OpenTelemetry Span 与模型/动作 Event 已实现。部署者可接入 Collector；
本版默认不启用外部遥测上报。日志、Memory 与动作结果可能含个人信息，应保护数据库和 workspace。
