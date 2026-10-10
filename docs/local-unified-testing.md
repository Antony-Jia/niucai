# 本地 Docker 统一会话联调

使用独立 Compose 项目 `niucai-local-unified`，不修改现有本地 niucai 或远端服务。数据库、workspace、Pi 会话、桌面 home 和浏览器 profile 都使用项目独立卷；不复用 `niucai_workspace` 等远端固定卷名。

## 启动

从仓库根目录执行 PowerShell 命令，先确认 Docker Desktop 正常运行：

```powershell
& services/kernel/.venv/Scripts/python.exe services/kernel/scripts/configure_local_stack.py
docker compose --env-file build-cache/local-unified/.env -f deploy/compose/compose.local-test.yml config --quiet
docker compose --env-file build-cache/local-unified/.env -f deploy/compose/compose.local-test.yml up -d --build
```

首次创建独立测试密码和 Token，并复用已配置的模型提供商。配置保存在 Git 忽略的 `build-cache/local-unified/.env`；重复执行不会重置凭据。新电脑镜像首次下载较大，Worker 会等待 Computer 健康。

| 入口 | 本机地址 |
| --- | --- |
| Kernel | http://127.0.0.1:38080 |
| 手机 Web 页面 | http://127.0.0.1:38000 |
| 原生客户端桌面地址 | http://127.0.0.1:36901/vnc.html |

所有宿主端口只绑定回环。CDP 仅开放于独立 Docker 网络内 `computer:9223`；浏览器、Worker 与 VNC 操作同一个 Chrome 实例。

客户端通过 Settings 连接 38080，Token 使用本地测试配置的 `NIUCAI_API_TOKEN`。VNC 使用 `kasm_user` 和独立 `VNC_PW`，不是 Kernel Token。

## 真实 API 验收

从仓库根目录执行：

```powershell
& services/kernel/.venv/Scripts/python.exe services/kernel/scripts/verify_local_stack.py --config build-cache/local-unified/.env --report local-logs/local-unified-api.json
```

脚本只针对固定本地 38080：注册 local-unified、创建独立会话，验证幂等消息、纯讨论、运行中补充要求、实际浏览器读取、旧审批失效、一次真实文件写入和分页时间线。仅审批测试 workspace 内 `local-unified-proof.txt` 写入，不放开 Shell。

它将注册所得 `NIUCAI_COMPUTER_WEB_ID` 保存到测试配置；首次运行后重新创建 API，使手机电脑代理读取电脑 ID：

```powershell
docker compose --env-file build-cache/local-unified/.env -f deploy/compose/compose.local-test.yml up -d --no-build api worker web
```

手机 Web 使用 HttpOnly Cookie，通过 Kernel 代理访问 VNC；只有接管期间允许连接，交还后断开并拒绝输入。本地回环 HTTP 配置关闭 Secure Cookie，仅用于本机开发，不用于公网部署。

## 停止与保留数据

```powershell
docker compose --env-file build-cache/local-unified/.env -f deploy/compose/compose.local-test.yml stop
```

不执行 `down -v`，保留会话与产物。原生客户端切换前的连接配置备份位于本机 `build-cache/local-unified/connection-before-local.json`，原远端凭据仍在 Windows 凭据管理器中；可通过 Settings 切回原地址。

实际验收结果见 [统一会话验收记录](unified-conversations-validation-20261010.md)。本地链路通过不代表远端网络、镜像发布或服务器部署已通过。
