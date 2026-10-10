# 第二阶段本地客户端联调结果

日期：2026-10-10。执行端：Windows 本机；目标：101.126.159.10 隔离栈。

## 结论与验证边界

本机已独立连通隔离服务，并从桌面 React 界面完成 Chat 转任务、浏览器执行、
截图产物、审批、暂停恢复、停止、人工接管及交还、刷新恢复状态与真实 SSH
断线重连。核心任务链路通过。

真实界面测试使用 Chrome 运行桌面源码，通过已有 BrowserBridge 与本机临时代理
连接真实隔离 API。没有使用示例数据、假 Worker 或假模型；凭据仅保留在临时代理
进程内，不进入浏览器、脚本参数或记录文件。本测试不覆盖 Windows 原生 WebView、
Tauri IPC 事件链、凭据管理器、系统保存文件对话框、内嵌桌面的人工键鼠输入。
不能把浏览器界面测试等同于原生客户端全链路验收。

## 环境核对

- 隔离 Kernel：本机/远端回环 28080；桌面：26901。
- Computer：`7be76444-365d-418f-be76-cade00374cf2`（iso-main）。
- API/Worker 镜像均为 `niucai-integr-kernel:95b1daa`，ID 均为
  `sha256:b8b34370303c5b3666d2b6b36c8dee235f12c5d43c50f914c9099c4966ba988f`。
- 健康接口 200；无 Token 的任务接口 401；认证后的任务、电脑、对话接口正常。
- SSH 指纹核验通过。独立 integration 隧道保留，不覆盖生产连接配置。

## 本地检查

- Vitest：21/21 通过。
- TypeScript + Vite build：通过。
- 浏览器示例界面 E2E：4/4 通过。这四项单独标为示例检查，不计真实服务验收。
- Rust 单元测试：3/3 通过，不代表原生窗口交互已测。
- 修复 `task-result.test.tsx` 使用了不支持的 `getByRole` 参数导致构建失败的问题。
- 修复 `start-remote-tunnel.ps1` 在 Windows PowerShell 5 把 ssh-keyscan 的正常
  stderr 提示当作终止错误的问题。现在检查退出码、唯一主机公钥和原定指纹，
  保留所有诊断记录，没有降低主机身份校验。
- Windows PowerShell 5 实际启动成功；使用错误预期指纹的反例验证被拒绝，未创建隧道。

## 真实界面与任务

| 验证 | 结果与证据 |
| --- | --- |
| Chat 与任务分离 | Chat 消息持久化，点击转交前任务列表没有新增任务 |
| Chat 转交 Agent | 指定 iso-main，任务 `c5eb1b47-4201-422c-9936-9a59d98a8f2f` |
| 浏览器完整执行 | 独立 URL，navigate、snapshot（标题 Example Domain）、screenshot 均 SUCCEEDED，任务 COMPLETED |
| 截图产物 | 认证 preview 返回 PNG data URL；下载 200，PNG 签名正确；截图保存并复核 |
| 接管 / 交还 | 界面操作驱动真实服务 AGENT → HUMAN → AGENT |
| 拒绝审批 | `4a142004-fab1-4233-bbbd-9cc77350b811`，动作 DENIED；远端对应文件不存在 |
| 暂停、批准、恢复 | `4248fedb-98c5-4bbb-9a0e-ff21062e957d`；暂停后批准仍 PAUSED；明确恢复后 COMPLETED；只执行一次 files.write，文件内容正确 |
| 停止任务 | `488441c2-9a39-468b-be5b-5024b6c53af6`，确认停止后 CANCELLED；动作数没有增长，文件不存在 |
| 页面重新打开 | 从真实服务重新加载已取消任务，无需本地缓存 |
| SSH 断线恢复 | 实际停止 integration 隧道，界面标记状态待同步；重新建立隧道后恢复权威状态，任务未重启 |
| 桌面 HTTP | 未认证 401；已核验的 kasm_user 与隔离 VNC_PW 登录返回 200 |
| 桌面 WebSocket | binary 子协议 + 正常浏览器 Origin 完成握手，收到 12 字节 RFB 协议头 |

首次浏览器任务 `c752f22b-ed63-4a89-be15-492bd31e6c3d` 虽 COMPLETED，但只执行
截图并复用旧页面状态，没有通过完整动作链检查。保留反例后，使用独立 URL 和
明确的新观测要求重测通过；未把第一次成功截图误记为完整验收。

最初桌面探针使用不正确的配置凭据失败；之后使用独立验证过的实际桌面凭据通过。
缺失 Origin 的 WebSocket 探针返回 404；补齐正常浏览器请求参数后握手通过，
没有改动远端 Nginx 或 KasmVNC。

## 剩余事项

1. 原生 Windows 客户端需手工核验内嵌桌面键鼠、保存文件对话框及关闭重开行为。
   本机工具不支持原生窗口操作，本轮没有把这几项记为通过。
2. 隔离 `.env` 的 `NIUCAI_COMPUTER_WEB_USER` 不在实际桌面账户列表中，
   `NIUCAI_COMPUTER_WEB_PASSWORD` 也与实际 VNC_PW 不一致。Windows 直接桌面入口
   可用，但移动端通过 Kernel 的桌面代理配置需 Hermes 对齐。不要把密码写入报告。
   此项本轮只核查，没有修改远端环境或重启服务。

本轮测试结束时隔离服务健康、电脑控制权 AGENT。拒绝/取消测试均已进入终态；
批准测试文件和截图保留为证据。未操作其他历史任务，未修改生产栈或 overcommit。

## 本地证据

`local-client/integration-tests/`（被 Git 忽略）保存 probe、live-ui、live-controls、
live-reconnect、remote-state、desktop-check、desktop-accounts 的 JSON 结果与截图。
对应测试脚本也在此目录；运行前需要独立隔离隧道及本机开发服务，不能直接指向
生产环境。临时测试代理和浏览器已结束；隔离隧道保留供后续原生客户端验收使用。
