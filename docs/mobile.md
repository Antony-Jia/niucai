# Android 手机控制端（V2 PWA）

手机与 Windows 共用 Kernel、任务、对话、审批和电脑状态。`apps/web` 是 React PWA；Android Chrome 可安装到主屏幕。它不会把 Android 手机注册为 Agent 可操作的 Device；Android Companion 属于后续 V3。

## 已实现

- 首页 Information Board、Chat、任务创建/暂停/恢复/取消、审批批准/拒绝。
- 云端电脑接管/交还、暂停/恢复、远程桌面嵌入与全屏。
- 文件下载（手机端上限 100 MB）、记忆管理。
- Android 竖屏、小屏、横屏布局，44px 触摸按钮、中文文本输入、软键盘视口和安全区域适配。
- HttpOnly 会话登录，七天到期；数据库保存会话摘要，浏览器不保存连接 Token。API Token 轮换会使现有手机会话失效。
- Cookie 修改请求及 WebSocket 校验同源 Origin。Bearer Token 接口继续支持 Windows。
- WebSocket 增量事件与轮询重连；回到应用时重新同步。服务器任务独立于客户端生命周期。
- Service Worker 仅缓存公开应用壳与静态资源，不缓存 API、文件内容或远程桌面；离线不排队执行动作。
- 运行中的页面可收到新审批系统通知。当前没有后台 Web Push；退出应用后需要回来查看审批。

## 服务器部署

在仓库根目录执行：

```bash
cp deploy/compose/.env.example deploy/compose/.env
# 编辑 .env：配置独立随机 POSTGRES_PASSWORD、NIUCAI_API_TOKEN、NIUCAI_DOMAIN。
# 需要模型执行时再配置 NIUCAI_OPENROUTER_API_KEY。
docker compose --env-file deploy/compose/.env -f deploy/compose/docker-compose.yml --profile public up -d --build
```

把域名解析到云主机，只开放 80/443 和按原方案保护的 SSH。Caddy 自动提供 HTTPS，同源 `/api/*`、`/computer/*` 和 `/websockify` 进入 Kernel，其他路径进入 PWA。数据库、Web 容器和 KasmVNC 不发布公网端口；API 的 8080 仅绑定主机 loopback。

在 Android Chrome 打开 `https://你的域名`，进入“更多页面 → 登录与安装设置”，输入 `NIUCAI_API_TOKEN`。再用 Chrome 菜单“安装应用 / 添加到主屏幕”。服务端会换发安全 Cookie；不需要把 Token 写入 URL。

现有数据库升级会新增 `browser_sessions` 表；Compose migrate 在 API/Worker 启动前执行。不要把 `NIUCAI_COOKIE_SECURE=false` 用于公网。

## 设备准备好后的远程桌面接入

1. 先完成 V0 的持久 Linux/KasmVNC/Openbox/Chromium，确保 Windows 与 Agent 使用同一 Chromium。手机端不创建第二个浏览器。
2. 在 Windows Computer 页或经过认证的 `POST /api/computers` 注册电脑，记录 Kernel 的 computer ID。
3. 在服务端 `.env` 设置：

```dotenv
NIUCAI_COMPUTER_WEB_ID=已注册的电脑ID
NIUCAI_COMPUTER_WEB_URL=/computer/vnc.html
NIUCAI_COMPUTER_WEB_UPSTREAM=http://computer:6901
NIUCAI_COMPUTER_WEB_USER=你的KasmVNC用户名
NIUCAI_COMPUTER_WEB_PASSWORD=你的KasmVNC密码
```

`computer:6901` 只是示例：上游必须在 API 可达的私有网络提供 HTTP，或提供受信任证书的 HTTPS。KasmVNC 不得直接面向公网。上游配置只允许 origin，不含路径、账号或 query；账号密码仅由服务器添加 Basic Auth，手机 Cookie 和 Kernel Token 不传给 KasmVNC。

4. 重建/重启 API 与 Worker 载入配置，再在手机点“接管电脑 → 打开远程桌面”。

Kernel 在 HUMAN 状态才允许桌面请求，支持 `/computer/websockify` 和部分 KasmVNC 版本使用的 `/websockify`。它在每个输入帧前检查登录与控制权，并定时关闭已撤销的长连接。交还 Agent、退出手机或会话失效会阻止后续输入；已经发往设备的输入无法撤回。页面隐藏、断线会关闭本地桌面视图，回来需重新打开。关闭视图不会自动交还控制权，避免意外让 Agent 在你未确认时继续。

触摸、双击、拖拽、滚动、键盘、剪贴板、缩放由 KasmVNC 工具栏提供；需要在真实 Android 手机上验证选用的 KasmVNC 版本，尤其中文 IME、剪贴板授权、横屏全屏和网络切换。设备尚未配置时，任务、Chat、审批可先使用，桌面入口会明确提示。

## 本地开发与验证

```bash
uv sync --project services/kernel
npm ci --prefix apps/desktop
npm ci --prefix apps/web
# 在 services/kernel 按 Kernel README 迁移并启动 API。
# localhost 开发才设置 NIUCAI_COOKIE_SECURE=false。
npm run dev --prefix apps/web
```

访问 `http://127.0.0.1:1421`；Vite 同源代理 API。真实手机安装需 HTTPS。生产包才注册 Service Worker。

```bash
npm run build --prefix apps/web
npm run format:check --prefix apps/web
cd apps/web
npx playwright install chromium
npm run test:e2e
```

E2E 在 loopback 启动真实 SQLite/FastAPI，模型回复使用测试 fixture，不执行外部设备或模型。覆盖 Pixel 7、360px 小屏、横屏的导航/布局，真实会话恢复/退出、任务生命周期、Chat、审批和离线应用壳；截图与报告由 `Android PWA CI` 上传。Kernel 测试另外覆盖 CSRF、会话轮换/过期、WebSocket 撤销，以及交还控制权后旧桌面连接无法继续输入。`e2e/server.py` 仅供测试，不用于部署。

真实服务器部署、实际模型调用、真实 KasmVNC/Android 触控与中文输入验收，需要设备准备好后联调；CI 中的手机浏览器模拟不能替代这些验收。
