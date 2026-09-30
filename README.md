# niucai — Personal Agent Computer Kernel

用户定义 Kernel，模型提出决策，Kernel 拥有状态与执行权。

本仓库实现 **V1 Kernel + Windows Desktop + Android PWA**。远程设备尚未配置时，可以先开发、测试和部署控制平面；
默认关闭设备动作，不虚构 Computer 在线状态。Windows Desktop 与 Android 手机 PWA 已提供；云端 KasmVNC 镜像与 Android Device Node 是后续阶段。

## 已实现

| 模块 | V1 行为 |
|---|---|
| Task / State | PostgreSQL 持久化、独立于 Chat、pause/resume/retry/cancel、checkpoint |
| Worker | `FOR UPDATE SKIP LOCKED` 领取任务、心跳、租约恢复、run token 阻止过期 Worker 写入 |
| Context Compiler | Identity / Goal / State / Plan / Memory / Computer / Recent Actions / Artifacts / Tools / Policy，裁剪可选内容 |
| Agent Runtime | DeepAgents + LangGraph 适配器；轻量 structured runtime 可切换；不拥有系统状态 |
| Model Router | planner / executor / browser / fast / vision / summarizer / guard / memory，按角色配置模型与层级 |
| Model Gateway | OpenRouter 统一请求、有限重试、模型事件、延迟、Token / Cost 使用信息 |
| Action Gateway | Pydantic 类型校验、策略、人工审批、审计、幂等键、动作执行前日志 |
| Human Control | AGENT / HUMAN / PAUSED，接管暂停关联任务并撤销 Worker 租约，交还后恢复 |
| Events | PostgreSQL 事件日志、REST 回放、认证 WebSocket、游标续传 |
| Memory / Artifacts | 工作状态、episodic / semantic 记录与相关性筛选、文件元数据与认证下载 |
| Computer Adapter | disabled / fake / local；local 接入私网 Chromium CDP，受控 workspace 文件与可选 Shell |
| 运维 | Alembic、Docker Compose、Caddy 模板、CI、单元与核心流程测试 |

## 项目结构

```text
apps/desktop/      Windows 优先的 Tauri 2 + React 客户端
apps/web/          Android 优先的 React PWA 手机控制端
services/kernel/
  src/niucai/
    domain/        类型化协议
    control/       Task 与 Computer 控制权
    context/       Context Compiler
    agents/        可替换运行时
    models/        Model Router + Gateway
    actions/       Policy / Approval / Audit / Executor
    storage/       SQLAlchemy 数据模型
    api/           REST + WebSocket
    worker.py      独立 Worker
  migrations/      显式数据库迁移
  tests/           核心流程与适配器测试
deploy/compose/    API + Worker + PostgreSQL
deploy/caddy/      HTTPS 代理模板
docs/             架构、API、接入、验收
```

## 本地运行（Python 3.12 + uv）

```bash
cd services/kernel
uv sync --frozen --extra harness --extra browser
cp .env.example .env
# 在 .env 中设置 NIUCAI_API_TOKEN，至少 32 字符；推荐 secrets.token_hex(32)。
uv run alembic upgrade head
uv run uvicorn niucai.api.app:app --host 127.0.0.1 --port 8080
```

另开终端，先填写 `models.yaml` 的 `planner` 与 `executor` 模型 slug、`.env` 中的 OpenRouter Key：

```bash
cd services/kernel
uv run --extra harness --extra browser python -m niucai.worker
```

默认 `NIUCAI_RUNTIME=deepagents`。`structured` 模式直接调用 Gateway 返回类型化决策。
配置缺失的任务会以明确错误进入 FAILED，补齐后可 retry。没有设备时可以创建纯推理任务。

```bash
export NIUCAI_TOKEN='your-random-token'
curl http://127.0.0.1:8080/api/tasks \
  -H "Authorization: Bearer $NIUCAI_TOKEN" -H 'Content-Type: application/json' \
  -d '{"title":"研究方案","goal":"给出 Personal Agent Kernel 的模块边界，不执行外部操作"}'
```

`NIUCAI_ADAPTER=fake` 仅供测试；返回值标明 fake，不代表真实电脑动作。
SQLite 仅用于本地单 Worker 调试；并发与部署使用 PostgreSQL。

## Docker Compose

```bash
cd deploy/compose
cp .env.example .env
# 填写两个独立随机密码、OpenRouter Key，并配置 services/kernel/models.yaml。
docker compose up -d --build
```

默认只在宿主机 `127.0.0.1:8080` 监听 API；数据库不映射公网端口。
有域名、DNS 和 80/443 可用后：`docker compose --profile public up -d`。
公网设备接入与 KasmVNC 暂不包含在此 Compose，详见 [设备接入](docs/device-integration.md)。

## 测试

```bash
cd services/kernel
uv run --extra harness --extra browser pytest -q
uv run ruff check src tests migrations
uv run ruff format --check src tests migrations
uv run alembic upgrade head
uv run alembic check
```

PostgreSQL 测试使用专用空数据库：设置 `NIUCAI_TEST_DATABASE_URL` 后运行 pytest。
**测试会清空该测试数据库中 Kernel 表，请勿指向生产数据库。** CI 覆盖 SQLite / PostgreSQL 两种配置。

## CI

GitHub Actions 自动检查 main push / PR，并支持手动触发：Python 检查、SQLite / PostgreSQL 测试和迁移、Docker 构建与 API 启动检查。无需模型密钥或设备。详情见 [CI 配置](docs/ci.md)。

## V1 边界

- 单用户 API Bearer Token 认证；REST 与 WebSocket 均认证。Username / Password / TOTP 登录界面尚未实现。
- DeepAgents 是每轮独立的提案 Harness，LangGraph 的执行过程不作为 Kernel 的权威状态；
  持久化的是 Kernel 的计划、提案、动作与任务检查点，尚未提供完整 Harness 内部暂停续跑。
- browser / vision 等角色可配置；当前主流程使用 planner + executor，不承诺自动复杂路由。
- Memory 使用 PostgreSQL 记录与词项相关性筛选；pgvector 基础镜像已选定，但 embedding 与向量检索未启用。
- Browser V1 支持 navigate / DOM snapshot / click / fill / scroll / screenshot；
  tabs / select / upload / download 的独立工具以及视觉坐标 fallback 尚未实现。
- local adapter 面向一台 Linux Computer，共享私网 CDP 与 workspace；Android 只预留类型，不可执行。
- 接管响应在当前有界动作结束后生效；当前最长动作约 30 秒。KasmVNC 客户端必须先调用 take-control。
- Shell 不是安全沙箱；默认关闭，显式启用后仍需人工审批，应只在专用 Computer 容器内运行。
- OpenTelemetry 已有模型与动作 span；默认不发送到外部服务，后续可配置 exporter。

详见 [架构](docs/architecture.md)、[API](docs/api.md)、[验收与已知限制](docs/acceptance.md)。

## Windows Desktop

`apps/desktop` 是 Tauri 2 + React + TypeScript 客户端，包含 Dashboard、Chat、Tasks、Computer、Files、Memory、Settings。支持 Kernel 连接、Windows 系统凭据保存、持久会话、任务管理、审批、接管/交还与产物下载。

安装与开发步骤见 [Windows Desktop 文档](docs/desktop-windows.md)。Windows x64 的 NSIS/MSI 安装包由 [Desktop CI](../../actions/workflows/desktop.yml) 构建。配置真实云端电脑之前，可使用明确标记的示例模式查看界面。

## Android 手机控制端

已加入 `apps/web` React PWA，复用 Windows 的任务、对话、审批和记忆页面；支持主屏幕安装、会话恢复、实时事件、接管/交还云端电脑。详见 [移动端部署与验收](docs/mobile.md)。真实桌面触控需在 Computer Runtime 配好后联调；后台推送和 Android Device Node 留待后续阶段。
