# 隔离环境就绪与联调入口

日期：2026-10-09。依据 Hermes 反馈核对本机交付记录，尚未从本机独立连接验证。

## 最新浏览器修复（95b1daa）

Hermes 已交付并报告隔离 API/Worker 更新到 `niucai-integr-kernel:95b1daa`。
本地核验 tar SHA256、镜像配置摘要及全部 31 个 Python 源文件一致，镜像内 Kernel
测试 109 通过、3 跳过，截图回归 4 项通过。远端真实浏览器任务
`4c82721b-e17e-4a4b-9556-faab851b1f2b` 日志记录 COMPLETED，截图已本地视觉复核。
下一步直接使用下述隔离入口做客户端联调，无需再构建或传输镜像。
详细证据与自动验收脚本的解析缺口见 `phase-2-browser-fix-review-95b1daa.md`。
以下 36e2183 信息保留为基线交付记录。

## 已确认的交付对应关系

- 反馈的归档 SHA256 与本地交付一致：`b8312f77c187e9a177f759c246fd03912286fc5bf1fee7dddc6c7269a656f62b`。
- 镜像配置摘要与交付一致：`sha256:ed83a65cf667fbd2a57935e259c707389818d8c64e2752d9c7a3a6b1b791de95`。
- revision 为 `36e2183a6e0e4391572ec7b92d23813213fb4e93`；Hermes 报告隔离 API/Worker 已使用相同镜像，新超时配置生效、健康与认证接口通过。
- 隔离 API 远端回环端口为 **28080**，桌面为 **26901**；Computer ID 为 `7be76444-365d-418f-be76-cade00374cf2`，名称 iso-main。
- 415 仅验证不支持的图片格式被拒绝，尚不能代替 PNG/JPEG 成功预览验收。

## 近端连接

从工程根目录执行以下命令，使用独立 integration 隧道，不覆盖默认生产隧道：

```powershell
.\start-remote-tunnel.ps1 -TunnelName integration `
  -LocalKernelPort 28080 -RemoteKernelPort 28080 `
  -LocalDesktopPort 26901 -RemoteDesktopPort 26901
```

客户端 Settings：Kernel 为 `http://127.0.0.1:28080`；Computer 为 `http://127.0.0.1:26901/vnc.html`。使用隔离环境 Token 和桌面凭据，不把凭据写入文档。选定 iso-main 后创建测试任务。

## 现在的分工

Hermes 直接开始文件任务、审批、暂停恢复、停止及可控超时的 Kernel/API 验证，无需等待浏览器问题解决。Chat 转交与原生客户端行为由 Windows 端配合验收；API 创建任务不能记为 Chat 转交已通过。

近端随后验证真实客户端连接、Chat 转交、审批入口、断线重连和关闭重开。两端记录同一 task/action ID，避免一边取消另一边正在验收的任务。

浏览器/桌面验收前：从隔离 API 和 Worker 容器读取实际 `browser_cdp_url`，分别验证该路径，并运行连接、导航、读取、截图四步；不能只用 computer 容器里 `127.0.0.1:9222` HTTP 200 推定 relay 路径可用。桌面 VNC 路径单独验证。Computer 的 OFFLINE 是业务观测状态，不足以判定服务不可用，也不应手改为 ONLINE 掩盖问题。

第四次复核的实际副作用调用审计与生产浏览器问题仍单独保留，不阻塞文件任务联调；不清空 Pi 转录，不切换生产。

## 交付行尾修正

原 Windows 导出的校验文件采用 CRLF，Linux sha256sum 把 CR 解析为文件名的一部分。已将本地校验文件改为字节写入 ASCII/LF，并生成 notes-v2 说明包。镜像 tar 的内容和 SHA256 未变化；远端已经完成校验和导入，无需重做部署。
