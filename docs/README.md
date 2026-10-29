# 工程文档索引

## 当前阶段

- [轻量 Kernel 与 Bot 白板重构方案](kernel-lite-redesign.md)：2026-10-29 拟议，P1 设备配对与拓扑已实现，其余未实现；Kernel 只保留状态、授权与编排 LLM，Bot（VPS / Windows 上的 Pi、Codex、Claude Code）通过白板领取卡片并回报结果，含设备配对、拓扑、skills/MCP 下发与 P0–P6 分阶段计划。落地后取代下列多 Bot 协作与 Kernel 内执行相关设计。
- [Kernel 任意位置部署与多 Bot 协作落地计划](kernel-anywhere-implementation-plan.md)：2026-10-13 方案；一期支持 Desktop 与 Kernel 同机，涵盖本地 Docker/VPS 接入、Bot 一次配置、主任务协作、分阶段实施与验收。

第一阶段已完成真实远端浏览器任务、人工审批、桌面连接及只读浏览器预览的链路验证。
第二阶段按下面三份计划实施；文档中的拟议字段和能力不表示已经实现。
K1/K2 与 L1/L2 首批实现已进入源码，进度合同见 [API](api.md)；远端切换和联合验收仍须另行完成。

- [远端环境与运维](phase-2-remote.md)：构建、发布、备份恢复、诊断与故障注入。
- [Kernel](phase-2-kernel.md)：进度合同、等待原因、租约/会话恢复、结果与产物。
- [本地客户端](phase-2-desktop.md)：状态展示、控制按钮、接管重连、结果交付。

实施顺序：远端 R1/R2 与 Kernel K1/K2 先行；客户端对接合同，再联合验证恢复与产物闭环。
统一验收场景和边界见上述三份文档。

## 开发与部署

- [远程环境安装、验收与排障手册](remote-environment-runbook.md)：重装入口，包含配置、镜像、桌面登录、任务等待、备份恢复及历史问题勘误。
- [Windows 开发与启动](development-windows.md)
- [远端部署与第一阶段基线](remote-deployment.md)
- [架构](architecture.md)、[API](api.md)、[设备接入](device-integration.md)
- [Windows Desktop](desktop-windows.md)、[移动端](mobile.md)
- [Pi Durable](pi-durable.md)、[验收](acceptance.md)、[CI](ci.md)

历史机器记录、旧源码包和 Hermes 交付副本保留在开发者本机，不作为当前源码或部署指令。

- [统一会话与执行架构](unified-conversations.md)：Chat/Task 合并、消息投递、Pi steering 与迁移。
- [统一会话验收记录](unified-conversations-validation-20261010.md)：自动回归、真实模型与 Linux Chromium 验证及部署边界。
- [本地 Docker 统一会话联调](local-unified-testing.md)：独立 API、Pi、PostgreSQL、Computer、VNC 和手机入口。
- [远程 Pi 执行节点 V1](remote-pi-nodes.md)：节点凭证、工作包授权、队列与租约、主 Pi 委派、双 Docker 节点模拟与恢复边界。
- [远程节点多执行引擎](remote-agent-engines.md)：方案（E0 Runner 重构、E1 Kernel 基础、E2 Codex 引擎、E3 Claude Code 引擎已实现，编排工具未实现），Node Runner 引擎抽象，接入 Claude Code / Codex 的事件映射、凭据与审批边界、迁移计划；未实现。
- [多资源节点接入验收](resource-nodes-validation-20261011.md)：Docker/VPS 接入向导、下载配置、节点管理、并发锁与独立 Docker 验证。
- [多 Bot 协作方案](multi-bot-collaboration.md)：Bot 身份、异步交接、群聊、并发 Worker 与 Routine 设计；B1–B5 代码已实现，部分实机验收待完成，详见该文关联验收记录。
- [Kernel Anywhere 合同](kernel-anywhere-contract.md)：实例身份、版本快照、连接模式、固定资源与二进制协议。
- [给 Hermes 的远程部署说明](handoff/hermes-remote-deploy.md)：服务器新装/升级、验收清单、备份回滚，以及让桌面同时保存本机与服务器连接。配套 `deploy/compose/remote-init.sh` 与 `make-remote-bundle.ps1`。
- [Kernel Anywhere 操作手册](kernel-anywhere-operations.md)：本机受管部署、VPS SSH 通道、独立验证与冷备份恢复。
- [Kernel Anywhere 本地验收](kernel-anywhere-validation-20261013.md)：本轮测试证据及未完成的现场验收。
- [YOLO、对话模式、依赖流程与技能/MCP](yolo-pipelines-extensions.md)：会话级免审批与对话模式、Bot 固定 VPS、按依赖传递结论与产物的多 Bot 流程、全局技能库与 MCP 服务器；自动化测试已覆盖，真实环境联合验收待完成。
