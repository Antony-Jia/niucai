# AGENTS.md

面向在本仓库工作的 AI 编码代理与新贡献者的总览。细节以 `docs/` 为准，本文只给出方向、边界和常用命令。

## 项目是什么

**niucai — Personal Agent Computer Kernel**：用户定义 Kernel，模型只提出决策，Kernel 拥有状态与执行权。
包含 Kernel 后端、Pi Durable 运行时、远端 Chrome/KasmVNC 电脑、Windows 桌面客户端与 Android PWA。
Android Device Node 尚未实现；设备动作默认关闭，不得虚构 Computer 在线状态。

## 仓库结构

| 路径 | 说明 |
|---|---|
| `services/kernel/` | Python 3.12 (uv)：FastAPI REST + WebSocket、Worker、SQLAlchemy、Alembic 迁移、测试。核心代码在 `src/niucai/`（`domain` 协议、`control` 控制权、`context` 上下文编译、`agents` 运行时、`models` 路由与网关、`actions` 策略/审批/审计/执行、`storage`、`api`、`worker.py`） |
| `services/pi-runtime/` | Node 24 + TypeScript，Pi Durable 私有 stdio 运行时（默认 `NIUCAI_RUNTIME=pi`） |
| `services/node-runner/` | 远程/资源节点 runner（Docker/VPS 接入） |
| `apps/desktop/` | Tauri 2 + React 19 Windows 客户端（`src/` 前端，`src-tauri/` Rust，`tests/` vitest，`e2e/` Playwright） |
| `apps/web/` | React PWA 手机控制端 |
| `computer/`、`relay/` | 可见 Chrome + KasmVNC 桌面；CDP/桌面私网中继 |
| `deploy/compose/` | Docker Compose（基础、computer、managed、remote-pi、anywhere-test）及 `verify-*.py` 验证脚本；`deploy/caddy/` HTTPS 模板 |
| `docs/` | 架构、API、验收、各阶段方案与验证记录，入口 `docs/README.md` |

## 核心概念

- **Task / Conversation**：Chat 与 Task 已统一为会话内“执行轮次”，状态持久化于 PostgreSQL，支持 pause/resume/retry/cancel、checkpoint。
- **Worker**：`FOR UPDATE SKIP LOCKED` 领取任务，心跳 + 租约恢复，run token 阻止过期 Worker 写入。
- **Action Gateway**：所有真实动作经 Pydantic 校验、策略、人工审批、审计、幂等键，且先记日志再执行。
- **Human Control**：AGENT / HUMAN / PAUSED；接管会撤销 Worker 租约，交还后恢复。
- **Model Router/Gateway**：按角色（planner/executor/browser/fast/vision/summarizer/guard/memory）配置模型，OpenRouter 或自定义 OpenAI 兼容地址。
- **远程 Pi 节点 / 资源节点**：主 Pi 委派、独立节点凭证、工作包授权、队列与租约（默认关闭，见 `docs/remote-pi-nodes.md`）。
- **轻量 Kernel 重构**（`docs/kernel-lite-redesign.md`）：节点 Bot 经白板领取卡片、Goal 由编排 LLM 推进（`NIUCAI_ORCHESTRATOR_ENABLED`）、Windows 本机节点作为 Desktop sidecar、Bot skills/MCP 随 Attempt 下发；P1–P5 代码已实现，实机验收待完成。旧的 Kernel 内执行链路可用 `NIUCAI_LEGACY_EXECUTION_ENABLED=false` 关闭，验收后删除。
- **多 Bot 协作**（B1–B5）与 **Kernel Anywhere**（本机受管/VPS 部署，见 `docs/kernel-anywhere-*.md`）：代码已实现，部分实机验收待完成。

## 常用命令

Kernel（在 `services/kernel`）：

```bash
uv sync --frozen --extra harness --extra browser
uv run alembic upgrade head
uv run uvicorn niucai.api.app:app --host 127.0.0.1 --port 8080
uv run --extra harness --extra browser python -m niucai.worker
uv run --extra harness --extra browser pytest -q
uv run ruff check src tests migrations && uv run ruff format --check src tests migrations
uv run alembic check
```

Pi runtime：`npm ci --ignore-scripts && npm run build && npm test`（`services/pi-runtime`；Pi 锁相关测试在 Windows 上跳过，用 Linux 容器运行）。

桌面 / Web（`apps/desktop`、`apps/web`）：`npm run build`、`npm test`（仅 desktop）、`npm run format:check`、`npm run test:e2e`；桌面开发 `npm run desktop:dev`，打包 `npm run desktop:build`。

本机一键：`.\start-local.ps1` / `.\stop-local.ps1`（需 Docker Desktop，详见 `LOCAL-DEVELOPMENT.md`、`docs/development-windows.md`）。

## 工作约定

- 开发环境为 Windows + PowerShell；后端在 Docker Linux 容器中运行。
- 文档与注释以中文为主，保持现有风格；改动行为时同步更新对应 `docs/`，文档中的“拟议”能力不代表已实现，勿当作事实。
- 数据库结构改动必须新增 Alembic 迁移，并保证 `alembic check` 通过。
- PostgreSQL 测试通过 `NIUCAI_TEST_DATABASE_URL` 指向**专用空库**，测试会清空 Kernel 表，绝不能指向生产库。SQLite 仅限本地单 Worker 调试。
- `NIUCAI_ADAPTER=fake` 仅供测试，返回须标明 fake。
- Shell 不是安全沙箱，默认关闭，启用后仍需人工审批。
- 不要提交密钥与机器本地内容：`.env`、`secrets/`、`build-cache/`、`downloads/`、`local-client/`、`local-logs/`、`niucai-delivery/`、`niucai-*.zip`、`*.db` 均已 gitignore；根目录的旧源码包/Hermes 交付副本不是当前源码或部署指令。
- 前端 Tauri 开发模式下 Vite 忽略 `src-tauri`，避免 Rust 编译中的 exe 引发 EBUSY。

## 已知边界（V1）

单用户 Bearer Token 认证（无登录/TOTP 界面）；单 Linux Worker 部署；备份需同时保存 PostgreSQL 与 `pi_sessions`；Memory 暂无向量检索；Browser 暂不支持 tabs/select/upload/download 与视觉坐标回退；接管在当前有界动作（约 30 秒内）结束后生效。完整列表见 `README.md` 与 `docs/acceptance.md`。

## 延伸阅读

`README.md` · `docs/README.md`（索引）· `docs/architecture.md` · `docs/api.md` · `docs/acceptance.md` · `docs/unified-conversations.md` · `docs/pi-durable.md` · `docs/remote-environment-runbook.md`
