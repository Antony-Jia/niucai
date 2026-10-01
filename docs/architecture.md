# Kernel 边界与恢复语义

Kernel 拥有 Task、Computer、Control、Approval、Action、Context、Memory、Artifact、Event 与 Model Policy。
DeepAgents / LangGraph 拥有持续推理循环、工具反馈和 Agent 会话检查点；
所有真实电脑动作通过 Kernel 包装工具调用 Action Gateway，无法直接调用 Playwright 或 Shell。

## 一轮任务

1. Worker 用 PostgreSQL `FOR UPDATE SKIP LOCKED` 领取 PENDING 或租约过期的 RUNNING 任务。
2. 为该轮生成 `run_token`，写入 TaskRun；独立心跳延长租约。
3. Context Compiler 读取权威状态与相关记录，产生 Context Package。
4. Planner 通过 Model Gateway 产生类型化 Plan；Kernel 验证并持久化。
5. DeepAgents 在 `thread_id=task.id` 的持续会话中选择工具；返回的反馈直接进入同一推理循环。
6. `execute_action(spec)` 通过 Action Gateway 校验类型、控制权和策略；高风险动作创建 Approval。
7. 执行器结果、Artifact、Event、Audit 存入数据库，工具向 LLM 返回状态和结果；LLM 可立即观察、纠错和调整计划。
8. `update_plan` 将初始 Planner 计划的修订持久化；`wait_for_human` 或动作审批 / UNKNOWN 触发 LangGraph interrupt。
9. Worker 在图检查点保存后将 Task 切换为 WAITING_HUMAN；条件满足后，用同一 thread 的 Command(resume) 继续。
10. 最终回答完成 Task。Kernel 的 max_steps 限制外部动作次数，图的 recursion_limit 限制单次内部循环。

`structured` 兼容模式仍保留逐轮 Plan / Decision 协议；默认 DeepAgents 模式不使用该协议驱动循环。

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

- LangGraph 检查点与 pending writes 保存到 harness_checkpoints / harness_writes，使用同一 PostgreSQL / SQLite 数据库。
- 保留完整父检查点历史和子图 namespace，以支持消息 DeltaChannel 与子 Agent 中断恢复。
- 默认模式 Action 由 `task_id:graph:hash(message_id, tool_call_id)` 幂等键定位；工具节点重放读取同一动作日志。
- 兼容 structured 模式使用 proposal 与 `task_id:current_step` 幂等键。
- 动作已提交但工具反馈未提交时，恢复消费动作日志，不重复执行；已计数动作也不重复递增 current_step。
- 所有图检查点写入都锁定 Task 并验证 run_token，旧 Worker 不能覆盖新会话。
- 图已保存最终回答而 Task 尚未完成时，恢复消费该回答，不再次请求模型。
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
Planner 先产生类型化初始计划；Executor 在持续会话中执行并通过 update_plan 调整计划。
DeepAgents 的内置文件工具只操作默认虚拟 Backend；真实 workspace、浏览器和 Shell 必须通过 execute_action。
显式编译的 general-purpose 子 Agent 继承父图的持久 Checkpointer，并复用相同 Kernel 工具、租约检查和动作串行锁。
运行中工具反馈直接返回 LLM，不为每次动作重建 Harness 或重新编译 Context。
每次领取 / 恢复任务编译一次当前 Kernel Context，并更新系统提示；每次模型请求前后检查租约。
人工接管撤销租约后，迟到的模型结果不会保存或执行，交还后从最后一个图边界恢复。

审批 / UNKNOWN 的 interrupt 只能由 Kernel 的持久动作状态解除。点击 resume 本身不授权动作。
UNKNOWN 必须经过人工 reconcile；拒绝审批作为 DENIED 工具反馈返回模型，允许选择其它方式。
工具校验 / 禁用 Shell 等明确失败可直接反馈并纠错；只读 browser.snapshot / files.read 的超时和错误直接返回 FAILED，允许即时重试或调整。
可能产生副作用的执行器已派发后异常仍保守按 UNKNOWN 处理；进程崩溃和取消保持人工核验。
动作预算耗尽时 interrupt，用户 resume 后由 Kernel 延长预算；内部循环预算耗尽也等待用户。

部署升级前暂停旧 Worker，执行 `uv run --no-sync alembic upgrade head`，再启动新 Worker。
旧任务没有图检查点时，从 Kernel Context 开始新会话；原有 Kernel proposal 不会变成新的图工具调用。
存在未完成的旧 structured proposal 时，应先用 structured 模式完成 / 取消该任务，避免跨运行时重放。

OpenTelemetry Span 与模型/动作 Event 已实现。部署者可接入 Collector；
本版默认不启用外部遥测上报。日志、Memory 与动作结果可能含个人信息，应保护数据库和 workspace。
