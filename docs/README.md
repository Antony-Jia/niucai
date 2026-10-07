# 工程文档索引

## 当前阶段

第一阶段已完成真实远端浏览器任务、人工审批、桌面连接及只读浏览器预览的链路验证。
第二阶段按下面三份计划实施；文档中的拟议字段和能力不表示已经实现。
K1/K2 与 L1/L2 首批实现已进入源码，进度合同见 [API](api.md)；远端切换和联合验收仍须另行完成。

- [远端环境与运维](phase-2-remote.md)：构建、发布、备份恢复、诊断与故障注入。
- [Kernel](phase-2-kernel.md)：进度合同、等待原因、租约/会话恢复、结果与产物。
- [本地客户端](phase-2-desktop.md)：状态展示、控制按钮、接管重连、结果交付。

实施顺序：远端 R1/R2 与 Kernel K1/K2 先行；客户端对接合同，再联合验证恢复与产物闭环。
统一验收场景和边界见上述三份文档。

## 开发与部署

- [Windows 开发与启动](development-windows.md)
- [远端部署与第一阶段基线](remote-deployment.md)
- [架构](architecture.md)、[API](api.md)、[设备接入](device-integration.md)
- [Windows Desktop](desktop-windows.md)、[移动端](mobile.md)
- [Pi Durable](pi-durable.md)、[验收](acceptance.md)、[CI](ci.md)

历史机器记录、旧源码包和 Hermes 交付副本保留在开发者本机，不作为当前源码或部署指令。
