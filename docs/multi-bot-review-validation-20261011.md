# 多 Bot 八项修复复核（2026-10-11）

首次复核检查用户后续修复，保留产品代码；新增 Pi 和 PostgreSQL 闸门测试，修正原 SQLite 多槽位测试对共享 PostgreSQL fixture 的错误依赖。当时没有迁移或重启业务环境；随后用户授权修复，完成情况见文末。

## 复核时发现的问题（后续修复见文末）

1. **P1：已应用迁移的数据库无法正常升级。** 本地业务库 `alembic current` 为 `c9d21011d003 (head)`，但 `information_schema.columns` 中 `work_items.conversation_id.is_nullable` 仍为 `NO`。修改同版本迁移不会再次执行，新版普通聊天根项写入 NULL 会触发 NOT NULL 约束。该迁移的 downgrade 会删除 Workflow/WorkItem/Handoff 等表，不能作为保留历史记录的升级方案。应新增 Alembic revision，修改约束并谨慎处理旧普通聊天根工作项归属；仅修改 nullable 也不会自动解除旧根项对聊天会话的占用。

2. **P2：人工恢复与超时调度存在竞态。** `CollaborationManager.tick` 只在扫描时仍为 WAITING_HUMAN 才顺延。截止时间已过而人工先将 Task 恢复成 PENDING 时，下一轮 tick 直接取消接收方。专用复现测试实际得到 `handoff=TIMED_OUT, task=CANCELLED`。应持久化人工等待起点，并在退出人工等待的事务中补偿等待时长，覆盖审批决定、resume、用户消息及电脑交还等恢复路径，而非仅在到期扫描时调整 deadline。复现文件在 `build-cache/multi-bot/review-edge-cases.py`，没有加入默认回归套件。

3. **P2：退出确认仍有假阳性。** 迁移将所有 `ended_at` 非空运行回填为已退出，但 `settle_task` 和人工 pause/cancel 会先写 ended_at、撤销租约，Pi 子进程的退出/锁释放在之后发生；EXPIRED 也不代表进程退出。`claim` 对非 Pi 运行仅因租约到期便填退出时间，也未确认旧 Worker/协程及外部调用已结束。租约 fencing 能阻止过期写入，不能证明资源已经释放。建议将旧记录标记为未追踪/待核实，或取得进程/锁退出证据后再写确认时间。

## 验证

- 用户改动的 Linux/WSL Kernel 全量：159 passed / 6 skipped，136.60 秒；覆盖真实 Pi 子进程。
- 补充 Pi 专项：5 passed，12.03 秒。新增电脑忙 PENDING→释放电脑→Pi 工具重放→完成，以及 BOT/ROUTINE 的模型可见来源说明与数据边界断言。
- 独立 PostgreSQL 容器、独立 `review` 数据库：协作与远程用例 36 passed，18.12 秒，覆盖多领取者、电脑互斥、同键发送、回复与等待竞争及远程节点领取。
- PostgreSQL 闸门专项：1 passed。两个 `FOR SHARE` 事务同时持有闸门；排他锁在持有者释放前因 lock_timeout 超时，释放后可提交 stopped。
- Ruff check 与变更范围格式检查通过，git diff --check 通过。全目录格式检查还有既有 `tests/test_browser_screenshot.py` 的两处格式问题；未作无关改动。
- 补充测试后的最终 Linux/WSL 全量：162 passed / 7 skipped，135.71 秒；7 个跳过项包含 PostgreSQL 专项与可选环境用例。PostgreSQL 两次专项运行合计 37 passed。

上述 PostgreSQL 用例不是完整并发验收：尚未验证所有相互交接死锁、停止与各写入口竞争、进程 kill 后恢复的排列。真实模型质量与远程节点端到端本轮没有复测。

## 改动文件

- `services/kernel/tests/test_multi_bot_pi.py`：新增 3 个参数化/流程用例。
- `services/kernel/tests/test_multi_bot_gate.py`：新增 PostgreSQL 闸门共享/排他测试。
- `services/kernel/tests/test_multi_bot.py`：SQLite 多槽位测试改为自行创建 SQLite Database，避免设置 PostgreSQL 测试 URL 后误报。

PostgreSQL 临时容器测试结束即停止；已有 `niucai` / `niucai-local-unified` 数据库与服务保持不变。当前运行镜像仍是之前启动时构建的版本，不能将网页上的运行行为当作这次修复已部署的证据。

## 后续修复与部署

用户授权修改以上问题，允许清空本地数据库。本次采用前向迁移，无需清空或 downgrade。

- 新增 `d0d21011d004`，升级已经应用的旧 `c9` 数据库：解除普通聊天根项的会话占用，保留专用执行会话及历史消息。
- Handoff 持久化 `human_wait_started_at`，在 Task resume、审批恢复、Workflow resume 等恢复事务中补偿实际等待时长；暂停期间保留标记，Workflow 总时长硬上限仍生效。
- 删除“ended_at/租约到期等于退出”的回填与判定。新增退出追踪起点，历史未追踪运行返回 `resource_release=unknown`，前端明确显示原因；新运行仅在执行协程结束或 Pi 锁确认为退出后写确认时间。
- 回归覆盖旧 NOT NULL 约束升级、普通/专用会话区分、历史退出元数据修正、恢复先于超时扫描、审批与 Workflow 恢复、过期运行与历史未知资源状态。

验证：WSL 全量 **166 passed / 7 skipped**（154.81 秒，包含真实 Pi 子进程）；后续增加的 Workflow 恢复参数用例另行复测。PostgreSQL 协作、远程及共享闸门 **40 passed**；独立 PostgreSQL 数据库中的前向迁移 **1 passed**。桌面/PWA 构建通过，前端 **28 passed**，Ruff 与变更范围格式检查、diff 空白检查通过。

最终扩展后的协作与前向迁移专项：WSL **31 passed / 2 skipped**；Task/Workflow 恢复用例在 PostgreSQL 上再次验证 **2 passed**。

已重建镜像并更新 `niucai-local-unified` 的 API/Worker（API 38080；开发网页 1421）。迁移前生成本地数据库备份 `build-cache/multi-bot/niucai-before-d0.dump`。实测数据库 head 为 `d0d21011d004`，`work_items.conversation_id.is_nullable=YES`；12 个任务、4 个会话及电脑登记保留。健康检查、鉴权协作状态、电脑列表、1420/1421 前端均返回 200，多 Bot 已启用、Worker 槽位为 3。旧运行缺少退出证据时返回 unknown，未伪造资源已释放。

以上覆盖本地部署与自动化回归；本轮没有新增真实模型质量或远程节点端到端验收。
