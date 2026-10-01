# V1 验收与后续验证

## 本地已经验证的内容

验证结果：24 passed，3 个 PostgreSQL 专用测试 skipped；Ruff 与迁移 schema check 通过。

- Task 创建、持续执行与完成；TaskRun 和 Event 持久化。
- 审批暂停、批准/拒绝后恢复，恢复使用原 proposal，不重复创建动作。
- Task pause/resume/cancel；旧 run token 不能推进任务。
- HUMAN 接管与交还；手工暂停不会被设备交还意外恢复。
- Computer 不能同时被两个任务领取。
- 过期租约恢复与心跳 fencing。
- EXECUTING 崩溃恢复为 UNKNOWN，不自动重复外部动作；人工 reconciliation 留审计。
- 动作幂等、不同参数冲突、stale DOM ref 拒绝。
- 严格 Pydantic Schema，额外字段拒绝。
- Context 相关 Memory 筛选；workspace 越界与符号链接越界拒绝。
- REST / WebSocket 认证、事件游标回放。
- ModelRouter role / reasoning、OpenRouter 请求、用量记录、限流重试（HTTP mock）。
- 真实已安装 DeepAgents 构建与调用流程，使用 fake Gateway 验证所有请求经过 Kernel。
- Alembic upgrade / schema diff 检查。

## 需要真实环境完成

- PostgreSQL 多 Worker 并发领取、行锁和 takeover 的实际阻塞时序；仓库 CI 已配置专用 PostgreSQL 测试。
- Docker Image 构建与 Compose 健康检查；仓库 CI 已配置 image build。
- OpenRouter 真实 Key / 模型能力 / Tool Calling / 计费字段验证。
- Chromium CDP、DOM ref 与页面变化、Screenshot 实际文件验证。
- 云主机重启后 Task / Computer / Browser Profile / Cookies / workspace 保持。
- KasmVNC mouse / keyboard / 中文输入 / clipboard / fullscreen。
- 接管中止下一次 Agent 操作、同 Browser 人机切换的实际验收。

不能把 fake adapter、mock LLM 或 SQLite 测试当作上述设备验收已通过。

## 交付范围

此次交付为 V1 Kernel 可运行核心，以及后续远程 Computer 适配接口。
Mobile PWA、Android Companion、复杂 RAG、embedding、完整用户登录体系
及 Remote Desktop 镜像尚未开发。


## Windows Desktop

七个页面、原生凭据存储、聊天持久化、任务与审批管理、控制权切换、产物下载与 Windows 安装包 CI 已实现。详见 [Windows 文档](desktop-windows.md)。真实 KasmVNC 与云端设备的中文输入、剪贴板及重启恢复等待设备配置后联调。

## 持续 Agent 会话验证

使用真实 DeepAgents / LangGraph、脚本化 Model Gateway 和假电脑执行器验证：

- 失败反馈进入同一推理会话，模型观察并调整下一步。
- 审批同意 / 拒绝在重建数据库引擎和运行时后继续，不重提动作。
- 未决审批不能被 resume 绕过，UNKNOWN 不能被 resume 自动重试。
- 模型调用期间人工接管，迟到结果不执行；交还后恢复图。
- 动作预算 interrupt 后恢复原工具调用。
- 子 Agent 工具审批保持子图检查点。
- 动作提交后、工具结果保存前崩溃，不重复执行或计数。
- 最终回答保存后崩溃，恢复不重复模型调用。
- 撤销租约的旧 Worker 无法写图检查点。

以上测试由 Kernel CI 在 SQLite 和 PostgreSQL 两个矩阵中运行，不使用真实模型密钥。
