# 本地联调版本验证记录

日期：2026-10-08。最终 Git 提交与可执行文件 SHA256 见交付目录 `local-client/phase2-integration/manifest.json`。

## 改动与测试范围

- 客户端：任务专属文件、摘要、动作成功数和失败/UNKNOWN 提示；PNG/JPEG 认证预览及下载；旧 Kernel 预览失败时提供下载提示。
- 画面：隐藏页面停止截图请求、恢复显示后更新、旧画面标记和手动刷新。
- 接管：桌面连接重试和退出入口；上方交还操作始终以 Kernel 确认为准。
- Kernel：保留前轮超时和中断改动，新增有界图片预览；修复 Worker 与 Pi 两层取消同时发生时打断子进程清理的问题。
- 联调：生产与隔离隧道可使用独立本地/远端端口及名称，防止覆盖同名活动隧道的 PID。

## 本轮验证

| 检查 | 结果 |
| --- | --- |
| Windows uv Python：产物接口、Chat/API 回归、运行时上限 | 9 通过（`PYTHONUTF8=1`） |
| Linux + SQLite 全量 Kernel 回归 | 105 通过，3 项 PostgreSQL 专项跳过 |
| Linux + 独立 PostgreSQL 17 全量 Kernel 回归 | 108 通过 |
| Desktop Vitest | 21 通过 |
| Desktop 浏览器回归（本机 Chrome） | 4 通过，包含结果区与任务文件、最小窗口、审批与接管示例 |
| Rust 1.98.1 原生单元测试 | 3 通过 |
| Windows Tauri 原生调试构建 | 成功，非安装包 |
| Ruff lint / format、Prettier、Git diff 空白检查 | 通过 |
| 两个 SSH 隧道脚本 | PowerShell 语法解析通过，未启动真实隧道 |

浏览器回归通过 `NIUCAI_TEST_BROWSER=chrome npm run test:e2e` 使用本机 Chrome；缺少 Playwright 缓存浏览器不再需要临时改测试配置。截图位于 `apps/desktop/test-results/`；已目视检查 `task-result.png`，结果与文件区沿用白色工作台风格。

Linux 验证使用现有本地 `niucai-api:latest` 镜像（ID `sha256:b46b6191088967ef840a9dccfba3284c69666dff91972bddefdf0289a2234ccb`），只读挂载最新 Kernel 源码和测试；临时容器以 uv 安装 pytest 9.1.1 / pytest-asyncio 1.4.0。未重新构建生产镜像，也未使用真实模型或远端浏览器。PostgreSQL 使用本轮新建网络和容器，不对现有数据库执行测试；临时数据库、网络已清理。

## 发现并修复的暂停恢复问题

Linux 首轮全量回归的 `test_lease_monitor_cancels_blocked_gateway_and_releases_writer` 失败；单项重跑亦复现：暂停后可退出，但恢复后仍为 RUNNING。

原因是 Pi 内层租约监测已进入清理时，Worker 外层监测再次取消执行，可能打断 `process.wait()` 及会话锁释放。现把 Pi 清理放入独立受 shield 保护的任务，等待子进程结束、RPC 协程回收和锁释放后再传播取消；同时处理进程已退出时的 kill 竞态。原失败单项修复后通过，随后 PostgreSQL 全量回归通过。

## 测试环境说明

Windows 首次运行下载文本测试时因默认非 UTF-8 写入而失败，设置 `PYTHONUTF8=1` 后通过。Windows 全量收集遇到 Pi 测试依赖 `fcntl`，因此 Pi 全量验证在 Linux 容器执行，没有将其记为 Windows 全量通过。

浏览器首次运行因本地 Vite 未持续监听而连接拒绝；显式启动本地 Vite 后，4 项重新执行全部通过。

## 尚待真实联调

这些结果不能替代真实模型超时、VNC 登录失败、人工接管交还、断开 SSH 隧道和关闭重开 Windows 客户端的联合验收。远端仍需部署相同提交的 API/Worker 至隔离栈；该版本尚未切换生产。具体矩阵见 [联调交付与验收](phase-2-integration.md)。
