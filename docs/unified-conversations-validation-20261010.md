# 统一会话重构验收记录

日期：2026-10-10。验证基于当前工作区源码与新增迁移，未切换远端 API/Worker。

## 自动验证

| 检查 | 结果 |
| --- | --- |
| Linux + SQLite 全量 Kernel | 123 通过，3 项 PostgreSQL 专项跳过 |
| Linux + 独立 PostgreSQL 17 全量 Kernel | 126 通过 |
| 连续两次失败后再重试的追加回归 | 1 通过；已确认旧 unanswered submission 不覆盖新答案 |
| 带历史数据的 Alembic 迁移 / metadata check | SQLite、PostgreSQL 均通过；保留原动作、审批、checkpoint 和任务 ID |
| Pi TypeScript 构建 / Node 测试 | 通过，4 项测试通过 |
| Desktop Vitest | 25 通过 |
| Desktop Chrome E2E | 4 通过，包含统一入口、会话执行详情、接管与最小窗口 |
| Android 浏览器 E2E | 12 通过，覆盖三种尺寸、Cookie 登录、消息触发执行、暂停恢复、审批和离线壳 |
| Desktop / Mobile TypeScript + Vite 构建 | 通过 |
| Ruff lint / 本轮文件格式 / Prettier / Git diff 空白检查 | 通过 |

全量 Ruff format 检查另发现原有 `tests/test_browser_screenshot.py` 两处格式差异；该文件不属于本轮改动，保留原状。Alembic 报告已有 path_separator 配置弃用警告，不影响迁移检查。

Linux 测试使用本地既有 Kernel 镜像，只读挂载本轮源码与重新构建的 Pi dist；测试数据库和网络单独创建，未使用现有业务数据库。手机 E2E 使用专用 unified-e2e.db 和 28081 端口，避开本机既有 8080 服务。

## 真实模型与浏览器

使用已有配置的真实 `deepseek-flash` 模型服务，Key 仅在内存和临时容器内传递。执行环境是独立 Linux Chromium、临时数据库、Pi 卷和文件目录；没有使用 FakeAdapter 或假模型。

通过以下连续流程：

1. 纯讨论保存验证词 blue，执行 COMPLETED，没有要求先生成计划。
2. 同会话发起浏览器工作，在实际模型请求期间接管电脑，执行进入 PAUSED。
3. 保存补充要求，交还电脑后恢复原轮次；真实 browser.navigate、browser.snapshot 均 SUCCEEDED，最终回答包含补充要求。
4. 同会话后续执行提出 proof.txt 写入，进入人工审批；批准后实际生成文件，读取文件并核对包含历史验证词。

四条用户消息均确认投递；不同执行共享同一 Pi conversation。可复现脚本为 [verify_unified_live.py](../services/kernel/scripts/verify_unified_live.py)，需 Linux、真实模型配置、Pi build 和 Playwright Chromium。

这次真实验收覆盖模型、实际浏览器与文件操作以及 Kernel 控制状态。没有覆盖 VNC 人工键鼠输入、远端共享云桌面部署、Windows 原生 Tauri 窗口或安装包。浏览器界面 E2E 使用示例或独立 API 测试数据库，不等同于真实模型界面联调。

## 实施注意

新增迁移为 `a7c21010d001`。部署顺序、备份和旧客户端 `/api/chat` 异步契约调整见 [统一会话架构](unified-conversations.md)。原有未提交的远程桌面、任务详情和测试修改继续保留。

## 追加：本地完整 Docker 工作台

随后按用户要求建立了独立 `niucai-local-unified` Compose 项目，实际构建当前源码的 Kernel、Pi、手机页面和 Kasm 桌面镜像。端口为 Kernel 38080、Web 38000、VNC 36901，均只绑定回环；原 8080 环境和远端未修改。部署与复现命令见 [本地联调说明](local-unified-testing.md)。

| 实际链路 | 结果 |
| --- | --- |
| 新镜像 + 独立 PostgreSQL 全量 Kernel 回归 | 127 通过，包含连续失败后成功重试与历史数据迁移 |
| 当前部署迁移版本 | `a7c21010d001 (head)`，migrate 退出 0 |
| Windows 原生客户端 → 本地 API → 真实模型 | 实际消息触发执行并收到 NATIVE_CHAT_OK |
| 原生暂停 → 追加消息 → 重启 Worker → 刷新客户端 → 恢复 | 原执行完成，补充要求 RESTART_OK 生效；暂停没有自动恢复 |
| 原生停止 → 同会话后续执行 | 取消执行保持 CANCELLED，新执行完成且引用 blue 历史上下文 |
| RUNNING 时强制终止 Worker，再启动 | 租约过期后原 Task 恢复；只有一条用户消息、一条助手回复，重复提交返回原 ID |
| 真实 Chrome + 运行中补充要求 | 导航并读取本地验收页面，STEER_OK 生效，多个执行共享 Pi conversation |
| 待审批时改变要求 | 原审批批准请求返回 409；重新提案批准后只有一次成功写入 |
| 实际文件与产物 | local-unified-proof.txt 内容正确，产物可下载；Worker 与 Computer 挂载的文件一致 |
| 原生嵌入 VNC + Windows 鼠标键盘 | 实际输入 HUMAN_VNC_OK 并提交；Agent 保持 PAUSED，交还后读取人工修改并完成 |
| 手机实际 Cookie 登录、模型回复与刷新 | HttpOnly 会话恢复，Token 未进入浏览器 storage，无横向溢出 |
| 手机真实 VNC 画面与控制权 | 接管前 403，接管后连接并收到真实桌面像素，交还后移除 iframe 且再访问 403 |
| 已建立的实际 RFB WebSocket | 交还后继续发送协议帧，被 Kernel 以 1008 关闭，无需依赖客户端自行断开 |
| 修复后桌面 Vitest / Chrome E2E / 构建 | 26 / 4 项通过，TypeScript 与 Vite 构建成功 |
| 原生客户端内置前端构建 | `tauri build --debug --no-bundle` 成功；无需 Vite，启动后实际认证 API 查询及事件 WebSocket 成功 |

新联调发现并修复了异步消息确认清空后续草稿的问题，新增了延迟确认期间保留输入的回归；也修正了长会话标题把“新建会话”按钮挤成竖排的布局。Dashboard 入口同步改为“新建会话 / 会话工作台”。

原生键鼠自动化初次遇到窗口焦点与 DPI 坐标问题，测试输入没有写入仓库文件。随后加入实际可执行文件、前景窗口、根窗口归属与 DPI 校验；对本地 VNC 的剪贴板读取权限设为拒绝，最终完成真实 Windows 键鼠 → VNC → 同一 Chrome 页面 → Agent 读取的验收。本机截图与不含秘密的报告保存于 Git 忽略的 `local-logs/local-unified-*`。

上述追加结果覆盖原生窗口和 VNC 键鼠，替代前文对应的未覆盖说明；仍未验证 Windows 安装包和远端更新后的服务。此前 Linux 临时 Chromium 与示例 E2E 的结果继续单独保留，不与本次真实联调混算。
