# 远程 Pi 执行节点 V1

Kernel 是统一控制平面；主 Pi Durable 负责规划、委派与汇总；VPS/本地 Linux 上的 Node Runner 驱动 Pi CLI 完成独立工作包。第一版只接 Pi，每节点一个执行槽。

## 使用

Kernel 执行 `uv run alembic upgrade head`，配置 `NIUCAI_REMOTE_PI_ENABLED=true`，重启 API 与主 Worker。默认关闭远程执行。

Desktop 的“远程 Pi”页面、手机工作台菜单的“远程 Pi 节点与任务”提供节点创建、提交、节点范围选择、授权、取消、重试、结果与文本产物查看。

主 Pi 获得 `remote_pi_nodes`、`remote_pi_submit`、`remote_pi_result` 三个工具。先提交多个独立工作包，再获取结果，可在多个节点并行。查询最长等待 20 秒；查询 WAITING_APPROVAL 子任务时主 Pi 持久化并进入等待人工，避免反复调用模型轮询审批。用户批准子任务后，在主会话点击继续，恢复原委派工具并聚合结果。

## 多 Docker 模拟

在仓库根目录运行：

```powershell
pwsh -File deploy/compose/remote-pi-demo.ps1
cd services/kernel
uv run python ../../deploy/compose/verify-remote-pi.py
```

独立 Compose 项目 `niucai-remote-pi` 包括数据库、API、主 Pi Worker、模拟模型服务和两个真实 Pi CLI 1.1.0 节点。API 为 `http://127.0.0.1:38180`。它使用独立数据卷，不修改已有部署数据库。

凭据保存在 gitignored 的 `build-cache/remote-pi/.env`，不打印到日志。模拟提供者是确定性测试服务，不代表真实模型推理质量；Pi RPC、工具执行、写文件、调度和持久化都走真实链路。验证覆盖：双节点并行、第三任务排队、文件和工具事件、取消、原节点重试、停机 LOST、重启核对旧 attempt、主 Pi 委派与聚合。证据保存为 `build-cache/remote-pi/verification.json`。

重复启动可加 `-SkipBuild`；代码更新后需要重新构建。停止模拟并保留数据卷：

```powershell
docker compose --env-file build-cache/remote-pi/.env -f deploy/compose/compose.remote-pi.yml down
```

## 接入实际节点

1. 在客户端创建节点，保存只显示一次的 ID/token；数据库只存 token 哈希。
2. 构建 `services/node-runner/Dockerfile`，在实际 VPS 或本地 Docker 的 Linux 容器中启动。参考模拟 Compose 的 runner 权限、只读文件系统、独立卷、tmpfs 和 init 配置。
3. 配置下表变量；节点不需要主 Kernel API token。

| 变量 | 用途 |
|---|---|
| `NIUCAI_KERNEL_URL` | 节点可访问的 HTTPS Kernel 地址 |
| `NIUCAI_NODE_ID` / `NIUCAI_NODE_TOKEN` | 独立节点身份 |
| `NIUCAI_PI_PROVIDER` / `NIUCAI_PI_MODEL` | 节点自身的 Pi 模型配置 |
| `NIUCAI_PI_BASE_URL` | 可选的 OpenAI-compatible `/v1` 地址 |
| `NIUCAI_PI_PROVIDER_KEY` | 自定义 endpoint 的模型凭据 |
| `OPENROUTER_API_KEY` 等 | Pi 内置 provider 的凭据 |
| `NIUCAI_PI_JOB_TIMEOUT` | 工作包最长秒数，默认 1800 |

Runner 主动通过 HTTPS 轮询连接，不需要公开 SSH/Pi RPC 或额外节点端口。生产调度使用 PostgreSQL。

## 权限与恢复

工作包先进入 WAITING_APPROVAL，用户批准后 Pi CLI 在**专用节点容器**内自主读写文件、执行 Shell。内部动作不逐项经过主 Kernel Action Gateway；Kernel 记录生命周期和 Pi 提供的工具事件。

Runner 使用服务身份，Pi 切换到 UID 10001，环境不携带节点/Kernel token，Runner 控制目录仅服务身份可访问。容器不挂载主机工作目录、Docker socket 或主 Kernel 凭据。禁用项目配置、扩展、技能、上下文文件和 MCP 自动加载。

这是每节点一个专用执行环境，不是每工作包一个强隔离沙箱。Pi 可能访问该节点以前的工作区与会话，不能混用不同信任域的任务；cwd 本身不是文件系统沙箱。

- 提交使用幂等键，执行使用 attempt_id；旧 attempt 无法写回新执行。
- 事件序号去重，保留全局事件游标以便回放。
- 取消进入 CANCELLING，Runner 停止进程组后确认 CANCELLED，再释放槽位。
- 租约过期进入 LOST，继续占用原节点槽位，拒绝迟到成功；不能直接重试。Runner 重连/重启停止旧进程、核对本地 ledger 后回报 FAILED，之后才能显式重试。
- 父任务 pause/cancel 撤销关联子任务；心跳也检查父任务是否仍允许执行。
- 重试保留原节点、工作目录和 Pi 会话，重新生成 attempt_id；不支持透明跨节点迁移。
- 不承诺外部动作 exactly-once；显式 retry 会提交继续执行的新轮次，需要核实既有副作用。

## 管理 API

管理 API 使用现有 Kernel 认证；`/api/remote/runner/{node_id}/...` 只接受该节点独立 token，无法访问 Kernel 管理接口或其它节点。

| API | 用途 |
|---|---|
| `POST /api/remote/nodes` | 创建节点，body 为 name；原始 token 只返回一次 |
| `GET /api/remote/nodes` | 在线状态、能力、容量，不返回凭据 |
| `POST /api/remote/jobs` | prompt、idempotency_key、allowed_nodes、可选 parent_task_id |
| `GET /api/remote/jobs?parent_task_id=...` | 子任务列表 |
| `GET /api/remote/jobs/{id}` | 状态、会话标识、结果 |
| `GET /api/remote/jobs/{id}/events?after=0` | 全局游标事件回放 |
| `POST /api/remote/jobs/{id}/approve` 或 `deny` | 工作包授权 |
| `POST /api/remote/jobs/{id}/cancel` 或 `retry` | 取消或重新进入审批 |

## V1 限制

- Runner 只支持 Linux，包括 Windows Docker Desktop 的 Linux 容器。
- 每节点一个任务，无账号配额调度或节点内部多槽并发。
- 最多回传 16 个文件元数据、64 KB 总文本、单文件 16 KB；大文件、二进制、完整代码库留在节点，尚无大产物上传和 Git patch 合并。
- 无运行中追加要求、任意会话暂停、跨节点迁移和 CLI 内部逐项审批桥接；生命周期使用取消、核对、显式重试。
- 领取已提交但 HTTP 回包丢失时，Kernel 保守保留 LOST 占用；本版没有人工强制释放功能。需核实节点执行状态后运维处理，不能自动重复执行。

Pi 接口参考：[官方 RPC 文档](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/rpc.md)。镜像固定 Pi CLI 1.1.0。
