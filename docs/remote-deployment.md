# 远端部署与第一阶段基线

基线日期：2026-10-07。第一阶段已由用户确认可以通过真实远端浏览器完成百度图片搜索。
这是单用户、单 Linux Computer、单 Worker 部署。后续可靠性工作见 [第二阶段远端计划](phase-2-remote.md)。

## 纳入版本管理的内容

| 路径 | 用途 |
| --- | --- |
| computer/ | KasmVNC 桌面、唯一可见 Chrome、浏览器与显示健康检查 |
| relay/cdp-relay.py | 同容器转发 Chrome 回环 CDP，并重写发现响应的 WebSocket 地址 |
| relay/desktop-relay.conf | 私网桌面中继，保留客户端认证与 WebSocket |
| deploy/compose/compose.computer.yml | 合并基础 Compose，连接 Kernel、Computer 与持久卷 |
| start-remote-tunnel.ps1 / stop-remote-tunnel.ps1 | Windows SSH 转发与主机指纹校验 |

这些 Computer/relay 源文件从现有远端读取并归档；Compose 镜像名改为变量，未把 .env、密钥、现有卷或现场运行日志加入仓库。
发布 Git 不等于自动部署。现有服务器使用临时补丁镜像，后续须按维护窗口合并配置、构建版本镜像并验证，不能直接覆盖运行环境。

## 服务与端口

```text
Windows Desktop -> SSH 22 -> Kernel 127.0.0.1:18080
                         -> desktop-relay 127.0.0.1:16901
Kernel / Worker -> Docker 私网 computer:9223 -> Chrome 回环 9222
desktop-relay   -> Docker 私网 computer:6901
```

只需开放 SSH；不把 PostgreSQL、CDP、VNC 或 API 直接映射公网。
用户操作与 Agent 控制同一个可见浏览器。浏览器截图是只读观察，不是完整桌面视频流。
桌面账户由基础镜像提供（已验证 kasm_user），密码由 VNC_PW 独立配置，不等于 Kernel Token 或模型 Key。
中继实际保留 HTTP Basic 认证；不要沿用旧交接关于“不使用 Basic”的结论。

## 新部署示例

需已有 Docker Engine 与支持 `!override` 的 Compose，实际参数先由部署者确认。

1. 复制 `deploy/compose/.env.example` 为 .env，生成 POSTGRES_PASSWORD、NIUCAI_API_TOKEN、VNC_PW。
2. 配置模型 Key/base URL 和 models.yaml；设置 `NIUCAI_ADAPTER=local`、`NIUCAI_BROWSER_CDP_URL=http://computer:9223`。
3. 构建 Kernel 镜像（从仓库根目录）：

```bash
docker build -f services/kernel/Dockerfile -t niucai-kernel:local .
```

4. 按模板启动并迁移：

```bash
cd deploy/compose
docker compose -f docker-compose.yml -f compose.computer.yml config --quiet
docker compose -f docker-compose.yml -f compose.computer.yml build computer
docker compose -f docker-compose.yml -f compose.computer.yml up -d --no-build postgres computer desktop-relay api worker
```

Kernel image 由 NIUCAI_KERNEL_IMAGE 指定，正式发布使用不可变版本 tag/digest。
模板使用固定网络名及数据卷名。已有同名环境时先审阅，不用此命令创建第二套环境；隔离演练需替换所有固定名称。
网络受限的旧服务器曾用 Dockerfile.local、预先下载的 uv 二进制和镜像源构建；这些机器缓存不随仓库发布。
标准 Dockerfile 需要访问 GHCR、npm、Python 包源；受限环境先验证构建依赖获取，不替换 uv.lock 为机器镜像地址。

启动健康后，通过带 Bearer Token 的 `POST /api/computers` 注册 Linux Computer，再用实际浏览器任务验证。
不要手动把 status 改为 ONLINE 来替代测试；也不要把 fake adapter 的结果作为真机证据。

## Windows 连接

运行根目录 `start-remote-tunnel.ps1`。脚本默认针对已验证的第一阶段服务器及指纹，可用参数指定已核实的主机配置。
Settings 使用 `http://127.0.0.1:18080` 和 `http://127.0.0.1:16901/vnc.html`。
先接管再打开交互桌面，结束后交还；Agent 模式仅显示只读浏览器预览。
停止隧道只影响客户端连接，不取消服务器任务。脚本不会关闭无关 SSH 进程。

## 已验证与尚未完成

- 真实聊天与模型网关、Pi 任务、审批、截图产物、持久化及认证链路已经联调。
- Chat 漏绑电脑问题已修复，原任务恢复后能导航/观测并进入正常审批。
- 本地只读浏览器预览及浅色客户端已构建验证。
- 全量故障注入、协调备份恢复、长期停滞告警、完整桌面只读流不作为第一阶段已完成项。
- Computer 基础镜像沿用现场 root + --no-sandbox 模式，只适合专用隔离环境；不承诺容器内浏览器沙箱保护。

后续发布前需记录 Git revision、镜像、配置差异和备份，并由三端联合验证；不能仅凭 healthy 宣布任务闭环完成。
