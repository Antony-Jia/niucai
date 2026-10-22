# 多 Bot 协作实现与验收记录（2026-10-11）

基于 [multi-bot-collaboration.md](multi-bot-collaboration.md) 实现。原方案文件保留用户已有修订，仅更新实现状态并链接此记录。此记录说明实际交付与验证边界。功能默认关闭，未修改运行中的服务或部署数据库。

## 实现范围

| 阶段 | 已实现 |
|---|---|
| B1 | Agent 校验、CRUD/归档、角色 Context、工具白名单及委派权限交集、节点准入、私有/共享 Memory、Desktop/PWA 角色与会话选择 |
| B2a | Workflow/WorkItem/Handoff、异步发送、all 等待、回复与自动完成回复、持久化收件箱、幂等载荷、租约 fencing、硬上限/超时、WAITING_BOT、审批恢复原因守卫 |
| B4 | 多执行槽位、短领取事务、同步数据库调用移至线程、电脑互斥/忙碌排队、等待释放电脑与槽位、Pi writer 锁、退出确认字段；SQLite 强制单槽位 |
| B2b | TRANSFER、显式路由、返工上限、any 等待、最终负责人判定、内容寻址 Artifact 快照与版本/hash、远程工作包授权附件下载及输出快照 |
| B3 | 2–6 成员、coordinator、@Bot/@everyone、广播逐项执行、展示与执行会话隔离、结果投影、仅同 Workflow 的公共输入摘要 |
| B5 | cron/时区 Routine、唯一运行记录、漏跑仅补最近一次、重叠跳过、停止闸门、内置看板与版本冲突检查、修改审计、Desktop/PWA 看板和 Routine 管理 |

新增迁移 `c9d21011d003`。旧 `a7c21010d001` 的数据迁移改用当时数据库表反射，避免升级历史数据库时查询尚不存在的新 ORM 列。迁移升级与 Alembic schema check 已回归。

全局停止会持久化闸门并取消受管任务/工作流/远程工作包。`GET /api/collaboration/status` 区分已请求停止与进程资源已确认释放；旧运行或失联节点未确认退出时保留 pending。解除闸门不会重建取消的执行，也不补发 Routine 积压。

## 验证结果

- Linux/WSL 完整 Kernel 回归：154 passed / 6 skipped（93.38 秒），含 DST/返工上限和真实 Pi 子进程用例。跳过项包含 PostgreSQL 专用并发用例和可选环境用例。
- 最新协作专项 Linux 回归：23 passed / 2 skipped，含真实 Pi 子进程双分支发送、all 等待、重新创建 Worker 后回放汇总、显式 bot_reply 原子提交与进程退出。
- Windows Kernel 目标回归：协作/远程/迁移 27 passed / 1 skipped；后续新增协作用例 21 passed / 2 skipped。Pi 依赖 Linux `fcntl`，其完整回归使用 WSL Ubuntu、Python 3.12、Node 24。
- Desktop：28 passed；Desktop、PWA、Pi Runtime TypeScript 构建通过。
- Pi Runtime：4 个原有 provider/输入版本测试通过；新增 Bot 工具的真实执行与恢复由 Kernel 的 `test_multi_bot_pi.py` 覆盖。
- Ruff 与 `git diff --check` 通过。迁移仍有既有 Alembic `path_separator` 弃用提示。

确定性模拟的实际 Workflow/WorkItem/Handoff/Task/TaskRun ID、最终结果及进程退出时间写入 `build-cache/multi-bot/verification.json`。该场景无远程 Job 或文件产物，不将其当作跨节点实机证据。模型为测试模型，证明协议和恢复，不证明真实模型协作质量。

## 启用与复测

先在 Kernel 目录运行 `uv run alembic upgrade head`，并重新构建 Pi Runtime。API 和 Worker 使用一致配置：

```dotenv
NIUCAI_MULTI_BOT_ENABLED=true
NIUCAI_WORKER_SLOTS=1
NIUCAI_ROUTINES_ENABLED=false
NIUCAI_HANDOFF_MAX_HOPS=4
NIUCAI_HANDOFF_MAX_PER_WORKFLOW=32
NIUCAI_WORKFLOW_MAX_MODEL_TURNS=200
NIUCAI_WORKFLOW_MAX_SECONDS=3600
NIUCAI_HANDOFF_TIMEOUT_SECONDS=900
```

SQLite 只允许一个槽位。PostgreSQL 才能设置 `NIUCAI_WORKER_SLOTS=3`。Routine 需另设 `NIUCAI_ROUTINES_ENABLED=true`。存在 ACTIVE Workflow 时关闭多 Bot，Worker 会拒绝启动，应先暂停/停止并处理已有流程。

Bot `tools` 默认无权限，`allowed_nodes=[]` 默认禁止远程执行；迁移后的 main 显式保留原权限。共享 Memory 写入另需 `memory.shared_write`。远程 V1 无细粒度工具审批桥接，对无法执行的工具权限交集直接拒绝工作包。

Routine 采用五字段分钟 cron，存储 UTC、按指定 IANA 时区调度。DST 缺失时刻推进至跳时后的首个有效时刻；重复时刻按时间顺序允许第二次 occurrence，唯一键使用 UTC scheduled_at。纽约 2026 年春季跳时和秋季回拨用例已覆盖。

隔离 Docker 演示已增加多 Bot 模式与确定性模型：

```powershell
./deploy/compose/remote-pi-demo.ps1 -MultiBot
uv run --project services/kernel python deploy/compose/verify-multi-bot.py
```

演示使用 PostgreSQL 三槽位、两个模拟节点，检查双分支汇总和 Research→Writer→Reviewer→Writer→Reviewer 路由，并写入相同证据路径。原 `verify-remote-pi.py` 保留远程节点审批、队列及恢复验证。新演示代码已静态校验，本次未运行 Docker 联调。

PostgreSQL 专用用例可通过 `NIUCAI_TEST_DATABASE_URL` 指向**专用可清空测试数据库**后运行 `uv run pytest tests/test_multi_bot.py tests/test_remote_pi.py -q`；fixture 会清空测试数据库全部业务表。新增用例覆盖多领取者/电脑互斥，以及同键发送/回复与等待竞争。

## 尚未验证的验收项

本机 Docker daemon 未运行，也无可用 PostgreSQL 服务。本次未提供 PostgreSQL 行锁/SKIP LOCKED、三槽位实际并发、互相交接死锁竞争、kill Worker 后旧 Pi 子进程恢复、全局停止与准入竞争的实机证据。对应 PostgreSQL 用例跳过。

跨节点附件协议在 Kernel 模拟节点用例中验证了父任务 WAITING_BOT、权限/节点白名单、版本/hash 冲突和受控下载；未运行两个真实 runner 的附件端到端演示。真实供应商模型及协作质量也未验证。

事件触发 Routine、Token 预留结算、多屏幕和远程 V1 逐项审批桥接仍按原方案排除。
