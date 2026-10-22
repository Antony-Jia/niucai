# 多资源执行节点接入验收（2026-10-11）

实现桌面与手机共享资源页面，支持本地 Docker/远程 VPS 分类、安装包、改名/分类、空闲启停调度与凭据轮换。
模型 API Key 只在节点本机填写。安装包从现有 Runner Dockerfile 构建，独立持久卷，不挂载 Docker socket。
新增迁移 `e1d21011d005`，既有节点归为 `unspecified`，保留 ID、凭据和任务关联。

## 自动验证

- Windows SQLite：资源、远程 Pi、multi-Bot、恢复迁移相关测试 45 passed / 6 skipped；跳过项依赖真实 PostgreSQL。
- 独立 PostgreSQL：资源、远程 Pi 和 multi-Bot gate 测试 18 passed，包含管理/领取并发、单槽占用和授权限制。
- 桌面前端全量：35 passed；包含两类接入、配置包、上线刷新、断开后清除凭据、LOST 管理限制和并发占用冲突提示。
- 桌面及 Web 的 TypeScript/Vite 构建通过。Rust `cargo check` 和 `cargo test --lib`（4 passed）通过。
- 新增/修改代码通过定向 Ruff、Prettier 和 Rust 格式检查。
- 浏览器生成 ZIP 经 Python zipfile CRC 和解压验证，解压后的 Compose 配置通过 `docker compose config --quiet`。

## Docker 实际链路

独立项目 `niucai-resource-test`、端口 38182、独立数据库/卷；未修改现有部署数据库。
运行 `deploy/compose/verify-resource-nodes.py`，验证：

- 两个真实 Pi CLI Runner 并行执行，第三个工作包排队。
- 占用时停用/轮换返回 409；空闲停用后仍在线，指定节点工作包排队，启用后完成。
- 旧 token 返回 401，新 token 更新原容器后重新上线并完成工作包，保留节点和数据卷。
- 节点停机后进入 LOST，停用、轮换、重试均返回 409；重启核对旧进程后进入 FAILED。

证据：`build-cache/resource-nodes/verification.json`（不含节点凭据）。

配置包运行验证使用从前端导出的 ZIP Compose；为隔离测试填写节点凭据与模拟模型配置，
接入验收专用 Docker 网络。Runner 实际上线并完成已批准工作包，证据为
`build-cache/resource-nodes/package-runtime.json`。凭据仅保存在 gitignored 的本机 `.env`。

## 验证边界与启用

以上模型为确定性模拟，Pi RPC、工具、文件、队列和持久化走真实链路；不代表真实模型质量或真实 VPS 验收。
未执行真实 VPS 部署或真实模型调用，也未进行安装后 Windows 原生保存对话框的人工验收。
现有 Kernel 与客户端部署未更新；启用前需部署代码、运行 `uv run alembic upgrade head`，
配置 `NIUCAI_REMOTE_PI_ENABLED=true` 并重启 API 与 Worker，再构建/更新客户端。
节点接入步骤见 [远程 Pi 节点](remote-pi-nodes.md)。
