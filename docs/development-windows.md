# Windows 开发与启动

后端 Pi Worker 依赖 Linux 文件锁，在 Docker/Linux 中运行；Windows 负责 Tauri 客户端与前端开发。
Python 环境统一使用 uv。第一阶段验证环境为 Python 3.12、Node 24、Rust MSVC 和 Docker Desktop Linux 引擎。

## 安装依赖

从仓库根目录开始：

```powershell
uv python install 3.12
Set-Location services/kernel
uv sync --frozen --extra harness --extra browser --python 3.12
Set-Location ../../services/pi-runtime
npm ci --ignore-scripts
npm run check
npm run build
Set-Location ../../apps/desktop
npm ci
```

原生构建另需 Rust MSVC、Visual Studio C++ Build Tools、Windows SDK 与 WebView2。
使用仓库锁文件，不把 .venv、node_modules、target、安装器和生成的 Tauri schema 提交 Git。

## 模型配置

复制 `deploy/compose/.env.example` 为 `.env`，生成独立的数据库密码与至少 32 字符 API Token。
模型供应商凭据只写 .env 或安全的秘密存储。自定义 OpenAI 兼容地址示例：

```dotenv
NIUCAI_OPENAI_API_KEY=<自己的模型密钥>
NIUCAI_OPENAI_API_BASE_URL=https://api.deepseek.com
```

`services/kernel/models.yaml` 配置各角色 Model ID；第一阶段使用 deepseek-flash。
使用 OpenRouter 时配置它的 Key、base URL 和对应模型 ID，不能只改 Key 却继续使用其他供应商的模型 ID。

可选：根目录 `.env` 使用 `OPENAI_API_KEY`、`OPENAI_API_BASE_URL`、`OPENAI_API_MODEL`，供 `start-local.ps1` 同步。
脚本依赖已建立的 `services/kernel/.venv` 和 Compose .env；它会统一所有角色模型，分角色配置请直接运行 Compose。

## 本地后端

```powershell
# 在仓库根目录，已启动 Docker Desktop。
.\start-local.ps1
# 停止服务但保留数据卷。
.\stop-local.ps1
```

本地 API 默认 `http://127.0.0.1:8080`；adapter 默认 disabled，适合纯推理任务。
Settings 中填写此地址与本地 API Token。远端 API 18080 是另一套服务，避免混淆两个配置和任务数据库。

## 客户端开发与构建

```powershell
Set-Location apps/desktop
npm run desktop:dev
# 生产前端资源 + 调试原生构建，无需 Vite 常驻。
npm run tauri -- build --debug --no-bundle
# 生成正式安装包。
npm run desktop:build
```

调试可执行文件为 `apps/desktop/src-tauri/target/debug/niucai-desktop.exe`。
源码编译的新客户端包含审批、任务电脑选择、只读预览和浅色风格；历史下载的安装包不自动获得这些改动。
编译前关闭正在运行的同一路径可执行文件。客户端凭据由 Windows Credential Manager 保存，不写入 Git。

## 检查

```powershell
Set-Location apps/desktop
npm run format:check
npm test
npm run build
npx playwright install chromium
npm run test:e2e
```

Kernel 全量测试在 Linux 执行，包含 Pi 子进程与 symlink 行为。Windows 可运行不依赖这些能力的定向测试。
测试数据库必须独立；`NIUCAI_TEST_DATABASE_URL` 测试会清空 Kernel 表，不能指向真实部署。
Vite 中的示例模式用于界面测试，不证明真实任务执行成功。

## 本机文件边界

`local-logs/`、`local-client/`、`downloads/`、`secrets/`、`.env`、交付 ZIP 和 `niucai-delivery/` 均不提交、不进入 Docker 构建上下文。
旧机器配置记录保留在本机；当前工程入口以 docs 为准。远端连接见 [部署文档](remote-deployment.md)。


## 2026-10-29 客户端连接恢复与 Bots

- 保存连接按 Kernel 实例标识去重，读取旧配置时使用最后保存的地址，避免旧端口遮盖新地址。Token 仍保存在 Windows 凭据管理器中。
- 全局连接提示显示当前名称、地址、重新连接和设置入口；缓存数据为最后已知状态。
- Bots 默认管理 `/api/bots` 节点 Bot，与白板和 Goal 使用同一套数据；历史 `/api/agents` 配置保留在“旧版协作”中，使用独立缓存键。
- 新建 Bot 分为选择在线节点、配置角色两步；引擎取自节点实际能力。提供角色模板，默认折叠模型、技能、MCP 和并发配置。Windows 默认只读与开工审批保持不变。
- Kernel 或节点不可用时不允许保存节点 Bot，当前打开的表单保留已填内容；错误在表单内展示。表单关闭或离开页面不会持久化草稿。

- 会话专用 `workbench-layout` 仅作用于主内容区，不再给应用根节点切换主题类；侧栏、导航和顶栏在页面切换时共用同一组样式。

## 本机 Windows 节点心跳

本机 Runner 默认启用 Claude Code / Codex，不要求安装 Pi。Kernel 心跳接口接受
`executor=pi/codex/claude/none`；protocol 3 的实际引擎与权限以 `executors` 为准，
`none` 表示未启用引擎，不代表任何引擎可用。节点的目录白名单 `workdir_allow` 随能力保存。

若配对成功后仍显示“离线 / 引擎未上报”，先查看
`%LOCALAPPDATA%\niucai-runner\runner.log` 和 Kernel API 日志。
旧 Kernel 只接受 `executor=pi`，会拒绝本机 Runner 心跳并返回 HTTP 422；需更新 Kernel API，
无需重新配对或删除节点。引擎上报可用仍不代表模型已登录或真实任务已验证。

Codex Windows 沙箱探测使用 `codex sandbox -- cmd /c exit 0`，没有 `windows` 子命令。
旧探测命令会导致权限列表为空；修复后需重新构建 Runner sidecar 并重启本机节点。
2026-10-29 本机验证：Codex `0.165.0-alpha.1.2` 沙箱探测成功，心跳提供
`read/workspace`；Claude Code `2.1.307` 上报同样两个档位。此记录仅验证节点与引擎能力，
未验证真实模型任务完成。
