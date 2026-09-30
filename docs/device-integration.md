# 稍后接入远程 Computer

本轮只完成 Kernel；没有假设你的云服务器、域名、浏览器 profile 或设备凭据已经存在。

## 核心配置

1. 准备 Ubuntu 24.04 云服务器与 Docker Compose。
2. 复制 deploy/compose/.env.example，填写独立随机数据库密码、API Token、OpenRouter API Key。
3. 配置 services/kernel/models.yaml 的 planner / executor；模型 slug 从自己的 OpenRouter 模型列表选择。
4. `docker compose up -d --build` 启动 PostgreSQL、迁移、API、Worker。
5. 先创建无 computer_id 的推理任务，确认模型、状态、事件链路正常。

## Computer Runtime 合同

接入一台 Linux Computer 容器：KasmVNC + Openbox + Chromium。
Kernel Worker 不负责安装 VNC 或启动桌面。

- Computer 在同一私有 Docker 网络提供 CDP，例 `http://computer:9222`。
- Chromium 有且只有一个供人和 Agent 共享的 persistent context。
- Chromium 在 Openbox 桌面内运行；Playwright 使用 connect_over_cdp，不能再 launch 第二个浏览器。
- `/browser-profile`、`/downloads`、`/workspace` 持久挂载。
- Kernel 的 workspace volume 与 Computer 的 workspace 挂载对应。
- CDP 只在内网监听；较新 Chromium 需要独立 `--user-data-dir=/browser-profile` 才启用远程调试。
- 当前 local adapter 操作该 context 的第一个页面，尚无多设备 endpoint 映射和 tab 管理。

Computer 完成后设置 `NIUCAI_ADAPTER=local`、`NIUCAI_BROWSER_CDP_URL`，重启 Worker。
通过 `POST /api/computers` 注册一台 `{ "name":"main", "kind":"linux" }`，
以返回的 id 创建浏览器任务。

KasmVNC 访问应由未来客户端在完成 take-control 后开放输入；
用户直接进入 VNC 绕过 Kernel 时，Kernel 无法检测鼠标键盘行为。
先调用接管 API，再允许输入；交还后禁用人工输入并让 Agent 重新 snapshot。

## 文件和 Shell

local adapter 的文件路径只能位于 NIUCAI_WORKSPACE 内，拒绝绝对路径越界和符号链接越界。
files.write 与 shell.exec 都需审批；Shell 默认关闭。

不要把 `NIUCAI_ALLOW_SHELL=true` 用在含生产数据/凭据的 Kernel 控制平面。
后续应将 Shell 执行器放入隔离 Computer 容器，通过内部受认证的 Action Executor 服务调用。
当前 LocalAdapter 提供核心执行合同，尚未做远程 Shell RPC。

## 后续扩展

- Android 注册类型已预留，但当前 Gateway 拒绝 Android 执行。
- Android Companion 应主动 outbound WSS 连接 Device Gateway；不开放 ADB 5555。
- 实现新的 adapter.execute(spec, computer) 并保留 Action Gateway 策略/审批/审计边界。
- 配置多 Computer 时，需要 adapter 按 computer.id 选择 endpoint 和 workspace，不能复用本版单节点 local 配置。

公网只开放 80/443，SSH 使用密钥；PostgreSQL、CDP、VNC、ADB 不开放公网端口。
此处是接入合同，不代表 Computer Runtime / Remote Desktop 已完成验收。
