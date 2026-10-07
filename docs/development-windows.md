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
