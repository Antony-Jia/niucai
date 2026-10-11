# 多 Bot 协作方案（参考 Grok Bot）

状态：**方案草案，未实现**。文中的表、字段、工具和接口都是拟议设计。

目标：在不改变"Kernel 拥有状态与执行权"这一原则的前提下，把当前的单一 `main` Agent 扩展为多个持久化、有名字、有职责的 Bot。Bot 之间可以异步交接、在群聊中协作、并行执行，也可以按计划定时工作。

修订：2026-10-11，补充异步等待、协作工作项、群聊调度、权限及恢复规则。Bot 是持久化身份，不要求常驻进程，也不与 VPS 一一绑定；Pi 会话负责推理，VPS / 本地节点提供执行资源。原发起 Bot 不必在每次阶段交接后同步恢复。

## 1. 参考模型：Grok Bot 怎么协作

参考来源：xAI 官方文档 `docs.x.ai/grok-bot`（Bots、Message and collaborate、FAQ）和官方工程指南《Grok Bot for Engineering》（2026-09）。

| 机制 | Grok Bot 行为 |
|---|---|
| Bot | 常驻的命名队友，有职责、独立对话和随时间积累的角色记忆 |
| 共享电脑 | 同一账号下的 Bot 共享一台云电脑（文件、浏览器会话、登录状态）；每个 Bot 有自己的屏幕，同一时间只能做一个电脑操作任务 |
| 直接交接 | Bot 给另一个 Bot 发异步消息，对方被唤醒处理后再回复；交接过程显示在对话中；图片和文件只能这样直接发送 |
| 群聊 | 2–6 个 Bot 共享一个目标；`@Bot` 指定负责人，不 @ 时由 Bot 自行决定谁回应，`@everyone` 用于广播；审批请求不进群 |
| 组织原则 | 一个 Bot 负责端到端结果；出现稳定的专业角色时才增加 Bot；每个阶段只设一个负责人；外部操作放在审批之后 |
| 超出上下文 | 用共享看板（官方案例用 Notion）跟踪工作项，Bot 定时巡检 |
| Routine | 定时或事件触发，例如凌晨巡检代码、早上运营 Bot 和每个 Bot 一对一复盘规则手册 |

## 2. niucai 现状基线

| 能力 | 现状 | 位置 |
|---|---|---|
| Agent 实体 | `agents` 表已存在，但只自动创建 `main`；`definition` 只保存 runtime，没有参与 Context | `storage/db.py`、`api/app.py` |
| 会话绑定 Agent | `Conversation.agent_id` / `Task.agent_id` 已有，默认 `main` | `control/conversations.py` |
| Identity | Context Compiler 写死为 `"niucai personal agent"`，没有按 Bot 区分角色 | `context/compiler.py` |
| 子 Agent | Pi `task` 工具：每个父任务只有一个 child conversation，串行执行，不能递归 | `pi-runtime/src/main.ts` |
| 并行执行 | 远程 Pi 节点：主 Pi 提交工作包，人工审批，多节点并行，主 Pi 汇总 | `control/remote.py`、[remote-pi-nodes](remote-pi-nodes.md) |
| Worker | `run_once` 串行领取，同一时间只执行一个 Task | `worker.py` |
| 会话约束 | 同一会话最多一个未结束的执行；Pi 卷按 Conversation ID 定位 | [unified-conversations](unified-conversations.md) |
| 电脑互斥 | `Computer.active_task_id` 限制一台电脑同一时间只被一个任务使用 | `control/computers.py` |
| Memory | 全局共享，没有 agent 维度 | `Memory` 表 |

结论：Bot 的身份、交接、群聊、并发 Worker、Routine 都没有实现。不过消息收件箱、Pi steering、审批、租约恢复、远程节点这些基础都已具备，多 Bot 可以在现有机制之上增量实现，不需要重写。

复用边界：当前 `ConversationManager.pending/inputs/acknowledge` 只处理 `role=user`；`send` 遇到已有 Task 时会撤销动作提案。Task 完成前的收件箱检查、Pi Adapter 的输入版本检查也依赖用户消息。新增 Bot 输入必须一起修改这些链路，不能仅新增 `role=bot` 后调用原 `send`。现有审批恢复还可能将 `WAITING_HUMAN` 改为 `PENDING`，因此 Bot 等待需要独立状态和恢复条件。

## 3. 设计原则

1. **Bot 间的消息走 Kernel 收件箱，Bot 之间不直接通信。** 每条交接都是一条持久化的 `Message`，可回放、可去重、可审计。
2. **发送与等待分离。** 投递工作后立即返回；需要汇总时显式等待。Kernel 在现有 Worker 调度链路上增加工作项准入和唤醒，不让等待中的 Pi 占用执行槽位。
3. **一个工作项只有一个负责人。** 不同工作项可以并行；展示群聊与执行会话分离，同一执行会话仍只有一个未结束的 Task。
4. **审批与接管只属于人。** Bot 不能替人批准动作，也不能批准其他 Bot 的动作；审批请求总是回到会话中，由人处理。
5. **防止失控。** 交接有跳数上限、频率限制、幂等键，以及全局"停止全部 Bot"开关。
6. **状态和产物是事实依据。** Workflow 保存整体结果，WorkItem 保存阶段负责人和依赖；消息用于投递和展示，Memory 用于长期知识。Bot 只能在授权的路由、预算和工具范围内决定下一步。

## 4. 分阶段实施

### B1 Bot 身份与角色（基础，必须先做）

- `Agent.definition` 增加结构化 Schema（Pydantic 校验）：

  ```json
  {
    "runtime": "pi",
    "label": "Kernel 负责人",
    "description": "负责 services/kernel 的实现与评审",
    "instructions": "角色说明与工作规则（playbook）",
    "model_roles": {"executor": "executor", "planner": "planner"},
    "tools": ["browser.*", "files.read", "files.write"],
    "default_computer_id": null,
    "allowed_nodes": [],
    "memory_scope": "own+shared"
  }
  ```

- API：`GET/POST /api/agents`、`GET/PATCH/DELETE /api/agents/{id}`。`main` 不允许删除；有未结束执行的 Bot 不允许删除。
- Context Compiler：`identity` 改为来自 Agent 的 name、label、instructions；`available_tools` 取 Agent 允许的工具与全局策略的交集。
- 权限：工具注册、模型可见工具与 Kernel 实际执行三个层次都执行白名单交集。Action Gateway 在 propose 和 execute 时校验；`bot_*`、`remote_pi_*`、子 Agent、记忆与看板入口也必须校验，不能通过委派绕过限制。委派权限独立于本地操作权限，工作包实际权限不得超出用户授权与接收 Bot 权限的交集。远程 V1 无逐项权限桥接，不支持的细粒度限制应拒绝委派，不得声称已强制执行。
- `allowed_nodes`：空列表表示禁止远程执行；`null` 表示不额外限制节点，仍受全局准入和工作包审批约束；非空列表为节点白名单。缺省按禁止处理，仅为兼容迁移显式保留 `main` 现有能力。
- Memory：增加可空的 `agent_id` 列。`own+shared` 表示读取本 Bot 的记忆加上 `agent_id IS NULL` 的共享记忆。查询、创建、更新与删除都校验作用域；共享写入需要单独权限，消息中的 sender 不能用于冒充记忆所有者。
- 客户端：Desktop 和 PWA 增加 "Bots" 页面（列表、创建、编辑角色），新建会话时可以选择 Bot。
- 迁移：新增一个 Alembic 版本，为已有的 `main` 补上默认 definition，保持既有工具权限。历史记忆先保留 shared 语义，不自动推断归属。Bot 使用归档代替删除已有历史；有未结束工作项或启用 Routine 的 Bot 不能删除。

验收：创建两个不同角色的 Bot，分别对话；Context 中的 identity 和工具列表不同；越权动作被 Gateway 拒绝。

### B2 Bot 间异步交接（核心）

#### B2.1 最小协作数据模型

一个协作任务不等于一个 Pi Task。Task 表示某个执行会话的运行生命周期；WorkItem 表示持久化的阶段工作；Workflow 表示整体目标。

| 表 | 最小字段与约束 |
|---|---|
| `workflows` | `id`、`origin_conversation_id`、`owner_agent_id`、`status`（`ACTIVE/PAUSED/COMPLETED/FAILED/CANCELLED`）、授权范围、允许路由、预算、最大修改轮次、`version`、时间戳 |
| `work_items` | `id`、`workflow_id`、`parent_work_item_id?`、`assignee_agent_id`、`conversation_id`、`task_id?`、目标及输出要求、输入/输出 Artifact 引用、`status`（`QUEUED/RUNNING/WAITING_BOT/WAITING_HUMAN/COMPLETED/FAILED/CANCELLED`）、`version`、时间戳 |
| `work_item_dependencies` | `work_item_id`、`depends_on_work_item_id`；组合唯一，禁止循环依赖，只引用同一 Workflow 的工作项 |
| `handoffs` | `id`、`workflow_id`、`from_work_item_id`、`to_work_item_id`、双方 Agent/Conversation ID、`from_task_id`、`to_task_id?`、`caused_by_handoff_id?`、`kind`（`DELEGATE/TRANSFER`）、`expects_reply`、`status`（`SENT/ACCEPTED/REPLIED/COMPLETED/FAILED/CANCELLED/TIMED_OUT`）、`hop`、唯一 `idempotency_key`、回复、截止时间、失败原因、时间戳 |

`ACCEPTED` 只表示已绑定接收工作项和 Task，不表示完成；RUNNING 等执行进度以 WorkItem / Task 为准。`REPLIED` 表示已提交回复，`COMPLETED` 表示不要求回复的接收工作已完成。每个工作项最多一个未结束执行 Task，数据库准入与租约保证唯一执行者，文件锁只保护 Pi transcript。

`messages` 增加 `sender_agent_id?`、`handoff_id?`、`workflow_id?`、`work_item_id?`、`routine_id?`、`source`（`USER/BOT/ROUTINE`）和 `kind`（`INSTRUCTION/ASSIGNMENT/RESULT/NOTICE`）。`role=bot` 可作为存储与展示标记，进入模型时转换为有明确来源的数据内容，不向模型 API 直接传入自定义 role，也不升级为 system 指令。Routine 输入同样不拥有 system 权限。

单 Bot 普通会话保持现有路径。协作执行使用独立会话，默认按工作项创建，不能把所有 A→B 请求塞进一个长期直连会话。相关会话通过 Workflow 关联，用户可以在一个视图中查看；Bot 的长期角色记忆独立保留。

#### B2.2 工具与两种协作模式

新增 Pi 工具（注册在 `niucai-kernel` 扩展中，`replay: "safe"`；所有身份、Workflow、跳数和授权字段由 Kernel 推导）：

| 工具 | 参数 | 行为 |
|---|---|---|
| `bot_list` | 无 | 返回允许协作的 Bot、启用状态、排队和运行数量；身份不要求有常驻进程 |
| `bot_send` | `to`、`content`、`kind`、`expects_reply`、`attachments?` | 创建工作项和交接，立即返回 ID；`expects_reply` 不会挂起发起方 |
| `bot_wait` | `handoff_ids`、`mode=all/any` | 已满足时立即返回结果；否则持久化等待依赖，进入 `WAITING_BOT` 并释放执行资源 |
| `bot_reply` | `handoff_id`、`content`、`attachments?` | 验证接收者身份和有效执行，原子保存回复及对应工作项的完成结果 |

- **并行委派：** A 向 B、C 发送 `DELEGATE` 工作，继续自己的工作，最后 `bot_wait(all)` 汇总。发起方保持原工作项责任。
- **阶段转交：** Research 向 Writer 发送 `TRANSFER`，同一事务保存当前阶段输出、完成当前工作项并创建下一工作项。Writer 可以转交 Reviewer；原 Research 不必恢复。Reviewer 通过后结束流程，或按允许路由创建修改工作项。第一版限定路由和修改轮次，不做任意 Bot 网状自动协作。
- `TRANSFER` 必须使用 `expects_reply=false`，并提交当前阶段输出和下一阶段输出要求；Kernel 通过独立 outcome 结束当前 Task，不让原 Pi 在转交后继续产生操作。`DELEGATE` 不完成发起工作项。显式回复和最终回答都进入同一幂等完成流程，Task 与 WorkItem 同步收尾，避免工作项已完成但原执行仍可写入。
- Workflow 完成由 Kernel 验证：有指定的最终工作项成功、必需依赖已完成、无未结束工作项；仅某个 Bot 输出最终回答不代表整个 Workflow 完成。结果投递到起始会话并展示，默认不唤醒原发起 Bot；需要汇总时显式创建汇总工作项。

#### B2.3 投递、等待与恢复

1. Kernel 在同一数据库事务中创建工作项、handoff、输入消息与事件，校验幂等载荷。Worker 通过数据库领取可运行工作，不依赖进程内通知。采用至少一次投递与接收去重，不声称模型或外部动作恰好执行一次。
2. 扩展收件箱的 pending、inputs、acknowledge、完成前未消费消息检查，以及 Pi Adapter 的版本检查。用户指令版本与协作结果分别记录；只有用户的指令变更执行现有提案失效策略，Bot RESULT 不自动撤销已审批动作。
3. 新工作进入独立执行会话。相关 RESULT 可注入其所属运行中的会话；ASSIGNMENT 不作为另一工作项的 steering。发给 PAUSED 会话的结果可以落库，但不能自动恢复。
4. `bot_wait` 持久化工具调用身份、依赖集合和 `all/any` 条件。Task / WorkItem 进入 `WAITING_BOT`，Pi 子进程结束、单写者锁与 Worker 槽位释放；电脑资源按有效所有者检查后释放。`any` 满足时不会自动取消其余交接，余下结果仍保留在工作项中。
5. 回复事务校验当前租约、接收 Agent、绑定工作项和交接状态；同时保存结果消息并检查等待条件。回复可以先于等待到达，进入等待的事务也必须再次检查已存在结果，避免丢失唤醒。
6. 依赖满足后，仅将确实等待这些依赖的 Task 恢复为 `PENDING`，由 Pi 持久化工具恢复返回结果。统一恢复守卫还检查 Workflow 暂停、全局停止、待审批和 UNKNOWN 动作；审批决定只解除对应审批，不能解除 Bot 等待。
7. 失败、取消或超时也是终态结果，满足等待完成条件并返回明确失败信息，不作为成功聚合。接收方最终完成却未调用 `bot_reply` 时，Kernel 使用其记录的最终结果补齐回复；执行失败则写入 FAILED。无回复要求的交接标记 COMPLETED。

#### B2.4 取消、权限与产物

- 交接幂等键使用 `task_id:pi:hash(tool task id)`；回复和转交也需幂等，重放时核对内容与 Artifact 版本。旧租约不得回复；已取消、超时的交接拒绝迟到成功，保留审计。
- `hop` 由因果交接继承并加一，不接受模型自报；Workflow 级总交接数、预算、时限和修改轮次防止通过新工作项重置限制。默认最大跳数 4，仅适合短链；长链需显式配置。限制同一对 Bot 的频率。
- 取消根协作任务等价于取消 Workflow，级联工作项、交接和关联远程 Job，撤销执行租约并终止进程。取消一个 DELEGATE 分支只影响其拥有的子树；TRANSFER 的生命周期属于 Workflow，不随已完成的前一阶段自动取消。暂停保存结果但停止新执行，恢复需人工操作。取消生效前已发生的外部副作用不会回滚。
- 用户可查看执行会话并插话；普通消息仅影响该工作项，取消整个流程使用明确的 Workflow 操作。整体拥有者、阶段负责人和当前执行者分别展示。
- 工作项结果引用可供相关 Bot 读取，展示消息不会自动创建新 Task。没有调用 `bot_wait` 的发起方不因每条回复自动唤醒；已完成会话只保存结果，需要继续推理时创建显式后续工作项。避免异步通知变成无条件回复循环。
- Attachment 只接受授权 Artifact 引用（ID、内容哈希/版本、媒体类型），Kernel 检查同一 Workflow 的访问范围。跨 VPS 使用受控下载/回传，不传本地绝对路径作为共享文件。输入版本固定，输出形成新版本；并发修改使用版本比较，冲突必须显式处理。
- 时间线记录 `handoff.sent/accepted/replied/completed/failed/cancelled/timed_out` 和工作项变更；多个展示会话引用同一事件 ID，避免重复投递触发新执行。

验收：A 先发送 B、C 再等待汇总；Research→Writer→Reviewer 无需原发起 Bot 每步恢复；回复早于等待也能继续；旧租约及非接收方不能回复；崩溃重放不重复创建工作项；超时、取消、审批和 UNKNOWN 不产生误唤醒。

### B3 群聊

- 新表 `conversation_participants`：`conversation_id`、`agent_id`、`role`（`coordinator`/`member`）。一个群最多 6 个 Bot，必须恰好有一个 coordinator。`Conversation.agent_id` 保留为 coordinator，兼容旧数据。
- 群聊是协作展示与用户入口，执行继续使用 B2 的独立工作项会话。工作项记录群聊来源，结果和交接事件投影到群中；每个执行会话有独立 Pi transcript。已有单 Bot 会话定位路径不变，不把历史 Pi 存储批量改为 `(conversation_id, agent_id)`。
- 共享上下文读取带发言者、来源、Workflow 和 Artifact 版本的群消息摘要；审批明细与机密不进入摘要。展示事件不作为可执行输入，避免 Bot 的每条发言自动唤醒其他 Bot。
- 路由规则（Kernel 判定，不交给模型）：
  - 消息 `@某Bot` 时由该 Bot 执行；`@everyone` 创建明确的一轮广播及去重的接收工作项，第一版依次执行，每个 Bot 最多提交一次对该广播的最终答复。广播不递归触发新广播。
  - 不 @ 时交给 coordinator，由它回答，或者用 `bot_send` 指派给群成员。
- 群里可以有多个未结束工作项，但每个工作项仍只有一个负责人。coordinator 等待成员回复时释放自己的执行槽位，不占用群级锁；成员可在独立会话运行，因此不会产生“负责人未结束，成员无法领取”的死锁。发言按数据库消息序号排序，执行并发受 B4 限制。广播依次执行是单轮路由策略，不是整个群的长期执行锁。
- 审批、登录和机密请求只出现在发起动作的工作项执行会话里；群里仅显示脱敏的等待状态和授权跳转链接。投影和客户端查询都检查访问范围，不能只靠前端隐藏。
- 客户端：会话页显示发言者头像和名称，输入框支持 `@` 补全；新建会话时可以多选 Bot。

验收：三人群中 `@` 路由正确；coordinator 等待时成员仍能执行；广播回复不产生唤醒风暴；群中和摘要中均无审批、登录明细；两个独立 Workflow 的输入不互相 steering。

### B4 并发执行

现有 Worker 串行执行，多个 Bot 同时工作时会互相排队。

- Worker 增加 `NIUCAI_WORKER_SLOTS`（默认 1，保持现有行为）。主循环维护最多 N 个 `run_claimed` 协程，每个槽位独立领取任务（`FOR UPDATE SKIP LOCKED` 已经支持）。
- 领取事务只处理准入与租约，不跨模型或网络等待。明确锁顺序并覆盖互相交接场景；同时操作两个会话时按稳定 ID 顺序加锁，防止 A→B 与 B→A 死锁。检查现有同步数据库与适配器调用，避免它们阻塞整个异步循环；不能仅用 `create_task` 就宣称具备实际并发。
- Pi 单写者锁（每个执行会话一个 flock）保护文件写入；工作项唯一执行、Task 租约和旧执行 fencing 由数据库负责。恢复时先确认旧 Pi 子进程退出并释放锁，再启动新写者。
- 电脑互斥沿用 `Computer.active_task_id`：电脑忙的任务留在 `PENDING` 队列，进度显示 `computer_busy`，不占用 Worker 槽位，不错误地变为 `WAITING_HUMAN`。结束或等待 Bot 时按当前有效所有者释放电脑；恢复后重新领取。纯推理与远程委派默认不绑定 Computer，确需电脑时再准入，避免仅有默认电脑配置就占住资源。
- 后续可选：每个 Bot 绑定独立的 Computer（多个 KasmVNC 容器），对应 Grok Bot 的"每个 Bot 一块屏幕"。本方案不包含多屏幕。
- 编码类重任务继续交给远程 Pi 节点，使用 B1 的 `allowed_nodes` 与权限检查。Bot 执行槽位、电脑和远程节点是三种资源，分别排队。V1 远程 Job 仍保留工作包人工审批；异步完成唤醒需要明确关联当前等待依赖，不能自动解除审批。单节点也可串行完成多 Bot 流程。

验收：在 `WORKER_SLOTS=3` 时，两个纯推理任务和一个电脑任务同时推进；电脑任务互斥；等待 Bot 不占槽位；队列长期等待可见；kill Worker 后旧进程退出、租约和 Pi 单写者恢复正确。单槽位也能完成异步链式转交。

### B5 Routine 与工作看板

- 新表 `routines`：`id`、`agent_id`、`conversation_id?`、`schedule`（cron）、`timezone`、`prompt`、`enabled`、`last_run_at`、`next_run_at`、重叠运行策略。第一版只支持定时触发，外部事件触发后续再做。
- 新表 `routine_runs`：`id`、`routine_id`、`scheduled_at`、`workflow_id`、状态；`(routine_id, scheduled_at)` 唯一。Worker 通过行锁领取到期 Routine，在同一事务创建运行记录、工作项和 `source=ROUTINE` 输入，再推进 `next_run_at`。Routine 文本为受控工作输入，不作为 system 指令。
- 明确时区与夏令时规则；停机漏跑只补最近一次，已有未结束运行时跳过新轮次并记录原因。并发 Worker、重启和全局停止都不能造成重复准入。定时调度默认只向原会话投影状态，执行使用独立工作项会话，不 steering 正在进行的人工任务。
- 看板不引入外部 Notion，直接展示 B2 的 Workflow / WorkItem，增加 labels、due_at 等查询字段。`board_list` / `board_update` 校验工作范围，更新携带 `expected_version`，保留操作者和前后值审计。不能通过 board_update 任意修改执行租约、审批或最终状态，状态变化走专用生命周期操作。Memory 保存长期知识，不作为工作项状态数据库。
- 运营 Bot（可选）：一个只有 `board_*`、`bot_send`、记忆读写权限的 Bot，按 routine 和其他 Bot 一对一同步规则手册（playbook）。更新规则手册必须走人工确认。

验收：每分钟一次的 routine 在 Worker 重启前后都不重复执行；看板项可以被多个 Bot 读写，并保留审计记录。

## 5. 接口与配置汇总（拟议）

| 类别 | 新增 |
|---|---|
| 表 | `workflows`、`work_items`、`work_item_dependencies`、`handoffs`、`conversation_participants`、`routines`、`routine_runs`；消息来源及关联字段、`memories.agent_id`、Artifact 版本元数据；持久化全局停止状态 |
| REST | `/api/agents*`、`/api/workflows*`（含 pause/resume/cancel）、`/api/work-items*`、`/api/conversations/{id}/participants`、`/api/handoffs?workflow_id=&conversation_id=`、`/api/routines*`、`POST /api/agents/stop-all`、`POST /api/agents/resume-all` |
| Pi 工具 | `bot_list`、`bot_send`、`bot_wait`、`bot_reply`、`board_list`、`board_update` |
| 事件 | `agent.updated`、`workflow.*`、`work_item.*`、`handoff.sent/accepted/replied/completed/failed/cancelled/timed_out`、`routine.fired/skipped`、`agents.stopped/resumed` |
| 配置 | `NIUCAI_MULTI_BOT_ENABLED`（默认 false）、`NIUCAI_HANDOFF_MAX_HOPS`、`NIUCAI_HANDOFF_MAX_PER_WORKFLOW`、`NIUCAI_HANDOFF_TIMEOUT_SECONDS`、`NIUCAI_WORKER_SLOTS`、`NIUCAI_ROUTINES_ENABLED` |

新增 Task 状态 `WAITING_BOT`；状态查询、进度、客户端类型、允许操作、租约回收、审批恢复和父子远程 Job 校验都需同步覆盖。禁止仅修改数据库字段而遗漏调用方。

`stop-all` 在事务中持久化停止闸门，并取消所有未结束协作 Workflow、Bot 执行和关联远程 Job；同时阻止新交接、自动恢复和 Routine 准入。所有写入入口及 claim 都检查闸门，避免停止与新任务竞态。等待执行进程退出后才报告资源释放完成；远程失联继续显示未确认停止，不伪造完成。`resume-all` 只解除闸门，不重建已取消任务，不自动补发停机积压；恢复仅允许人操作。该闸门影响所有受管 Bot（含 main），普通人工查看、审批和管理仍可用。

新能力默认关闭。关闭时保留 main 单 Bot 旧路径；Worker 槽位独立默认 1，Routine 必须同时满足多 Bot 开关与 Routine 开关。已有协作执行存在时禁止直接关闭功能，需先暂停或停止并处理；数据库迁移和历史记录读取不因功能关闭而失效。

## 6. 推荐顺序与依赖

```text
B1 身份与权限 ──► B2 工作项与异步交接 ──► B4 并发验收
                                            │
                                            ▼
                                         B3 群聊 ──► B5 Routine / 看板
```

推荐按 **B1 → B2 → B4 → B3 → B5** 实施。最小可用版本是 **B1 + B2 + B4（SLOTS=2）**，同时覆盖并行委派汇总和 Research→Writer→Reviewer 阶段转交。B3 是展示增强，不能作为交接正确性的前置条件；B5 看板复用 B2 工作项。先用三个逻辑 Bot、两个模拟节点验证闭环，无需先配置三台 VPS。

## 7. 测试与验收

- 单元测试：Agent Schema 和通配符权限、记忆作用域、委派权限交集、交接身份与幂等载荷、因果跳数、预算与修改轮次、群路由和 Artifact 访问。
- 流程测试（SQLite 与 PostgreSQL）：并行发送再 all/any 等待、链式 TRANSFER、分支失败和超时、根取消与分支取消、暂停后迟到回复、接收方完成未显式回复、审批与 UNKNOWN 守卫、不同 Workflow 会话隔离。
- 恢复与竞态：分别在发送事务提交后、等待状态提交前后、回复提交后但唤醒前、Pi 收件确认前后中断；重放不得重复创建工作项，不得丢失回复，旧租约不能提交结果。数据库提交与 Pi JSONL 不构成一个事务，依靠稳定消息 ID、确认和重放恢复。
- PostgreSQL 实际并发验证：多个 Worker 同时领取、A→B 与 B→A 交接、回复与取消竞争、回复与等待竞争、电脑互斥、版本冲突、全局停止与准入竞争。SQLite 不能作为行锁及 SKIP LOCKED 正确性的证据。
- Pi 运行时测试：`npm test --prefix services/pi-runtime` 覆盖新工具的 replay-safe 行为。
- 联调：扩展 `deploy/compose/remote-pi-demo.ps1` 的确定性模型，验证 A→B/C→A 汇总，以及 Research→Writer→Reviewer→Writer 修改→Reviewer 通过；验证无空闲节点时排队、单槽位链式推进、kill Worker 恢复。写入 `build-cache/multi-bot/verification.json`，记录 Workflow/WorkItem/Task/Job ID、结果与 Artifact 版本。模拟模型只能证明协议和恢复，不证明真实模型的协作质量。
- Routine：时区边界、漏跑只补最近一次、重叠运行跳过、并发领取去重、停机闸门；群聊：发言投影不执行、负责人等待不阻塞成员、审批机密不进入摘要。
- 验收记录按惯例新增 `multi-bot-validation-YYYYMMDD.md`。

## 8. 风险与待定问题

1. **成本失控**：除了跳数，还需按 Workflow / Bot 设置预算。复用 Model Gateway usage 时新增归属；并发请求前预留预算，完成后结算。远程 Pi V1 不经过同一计量入口，需先补 usage 回传或采用保守工作包次数和时限，不宣称统一 Token 硬上限已经生效。
2. **会话与数据恢复**：旧单 Bot Pi 路径不迁移，新增工作项使用新会话；恢复仍需配套备份 PostgreSQL、pi_sessions 和 Artifact 存储。数据库租约失效不代表旧进程已退出。
3. **共享资源的信任边界**：共享电脑包含登录状态；工具白名单不隔离账号与文件。远程 Pi V1 节点内也不是逐工作项强隔离。需要更强隔离时使用独立 Computer / 节点，不能把逻辑 Bot 私有记忆等同于完整运行环境隔离。
4. **已确定的第一版选择**：执行会话用户可见、允许插话；`@everyone` 依次创建并处理去重工作项；看板内置；固定允许路由、限定修改轮次；事件触发 Routine 暂不实现。
5. **实施前需在 Schema 中固化**：授权工作包的具体范围、Artifact 跨节点传输接口、预算默认值、Workflow 最终阶段判定与超时值。实现者不能靠提示词推断这些准入规则。

## 9. 本方案不做

- 多屏幕、多电脑自动分配；Android Device Node。
- 跨用户分享 Bot、团队 Bot、Bot 模板市场。
- Bot 之间通过消息直接传二进制大文件（使用授权 Artifact 引用及受控跨节点传输，不依赖各节点共享本地 workspace）。
- 远程 Pi 节点内部的逐项审批桥接（沿用 [remote-pi-nodes](remote-pi-nodes.md) 的 V1 限制）。
