# Pi 浏览器节点与 VNC

Pi 浏览器 Bot 操作 Chrome，KasmVNC 供用户观察、接管同一个可见窗口。执行在节点侧，Kernel 只分配卡片并保存事件和结果。本方案不实现 Pi 通用 MCP 桥接。

## 配置

`services/node-runner/browser-extension.ts` 注册 `browser` 工具，支持 `tabs`、`navigate`、`snapshot`、`click`、`fill`、`press`、`screenshot`。通过 CDP 连接已有 Chrome，不创建 headless 浏览器。`snapshot` 返回页面文字及可交互元素选择器，截图以图片返回 Pi。

仅在节点设置 `NIUCAI_PI_BROWSER_ENABLED=true` 时显式加载扩展；不扫描工作区扩展。浏览器专用节点关闭 Pi 内置 Shell、文件等工具，只启用 `browser`；卡片作业另加白板工具（`card_progress`、`card_submit` 等，见 [API](api.md)）。普通 Pi 节点不受影响。浏览器节点拒绝 `read` 档位，Bot 应使用 `workspace` 并启用开工审批。卡片审批覆盖整张卡，无法逐条拦截浏览器操作。页面内容视为不可信数据。

CDP 必须使用节点 loopback 地址，默认 `http://127.0.0.1:9222`。部署模板 [compose.browser-pi.yml](../deploy/compose/compose.browser-pi.yml) 通过 `network_mode: container:...` 与现有 Computer 共享网络命名空间，不发布 CDP 端口，也不经过旧 CDP relay。Computer 被重新创建后，需重新创建 Runner 以加入新网络命名空间。

在节点本地受保护的 `node.env` 中配置（不提交密钥）：

```dotenv
NIUCAI_COMPUTER_CONTAINER=实际的Computer容器名
NIUCAI_KERNEL_URL=https://你的Kernel地址
NIUCAI_PAIRING_CODE=一次性配对码
NIUCAI_PI_PROVIDER=你的provider
NIUCAI_PI_MODEL=你的模型
NIUCAI_PI_BASE_URL=https://你的兼容接口/v1
NIUCAI_PI_PROVIDER_KEY=节点本地模型密钥
NIUCAI_PREVIEW_URL=https://你的节点预览地址/vnc.html
```

先启动 Computer，随后执行：

```bash
docker compose --env-file node.env -f deploy/compose/compose.browser-pi.yml up -d --build
```

配对成功后，凭据保存在 Runner 的独立数据卷；后续可移除已使用的配对码。Kernel 地址需能从 Computer 的网络访问。本机 Docker Desktop 可以使用 `http://host.docker.internal:8080`。创建绑定该节点的 Pi Bot，标签可设置 `browser`、`vnc`、`pi`；单个 Chrome 建议节点槽位和 Bot 并发均为 1，避免多个任务同时控制窗口。

远程预览继续使用节点 HTTPS 与 KasmVNC 独立口令。仅本机回环调试可以使用现有 `http://127.0.0.1:16901/vnc.html` 转发；这不表示 VPS HTTPS 预览配置已完成。VNC 登录凭据不传给 Pi，Kernel 不保存预览口令。

## 本机启动

本次本机部署复用了已有 `niucai-remote-computer-1` 与 `niucai-remote-desktop-relay-1`，浏览器 Runner 为 `niucai-browser-pi-runner-1`，Bot 名称 `vnc-pi`。本机配置和重建命令记录于被忽略的 `local-logs/kernel-lite-startup.md`、`build-cache/kernel-lite-local/start-browser.py`。没有更新 VPS。

## 在桌面客户端查看远程画面

Devices 页的“远程画面”列表把可接管电脑与所有节点放在一起切换；节点登记了 `NIUCAI_PREVIEW_URL` 时，选中后显示“打开远程画面”（“执行环境”页的节点卡片仍有“查看远程画面”）。Windows 客户端调用 Tauri 命令 `open_node_preview(node_id)`：地址由命令自己从 Kernel 拓扑里取，前端不能传入任意 URL，并按连接地址同样的规则校验（HTTPS，仅本机回环允许 HTTP，不带凭据/查询/片段）。画面在独立窗口中打开，该窗口没有 Kernel 权限、令牌和注入脚本；KasmVNC 口令由用户在画面页里输入，不经过 Kernel 或 Pi。节点离线时按钮禁用。Web/PWA 端仍是普通链接。

本机重启 Docker 后，三个容器都会退出：依次 `docker start niucai-remote-computer-1 niucai-remote-desktop-relay-1 niucai-browser-pi-runner-1`（Computer 健康后再启动 Runner）。

验收应同时核对卡片成功、真实浏览器工具事件、Chrome 页面结果及 VNC/Chrome 健康，不能把 Runner 心跳当作浏览器可用的证明。
