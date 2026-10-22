# Kernel Anywhere 本地实施与验收记录

日期：2026-10-13（Asia/Shanghai）。本轮按用户补充要求先完成本地验证；VPS 地址 101.126.159.10 仅记录，未连接。

## 已实现

- P0：合同、API 与兼容矩阵，保护原有未提交工作；前向迁移清单和网络方式明确。
- P1：独立受管 Compose 项目/持久卷、Windows DPAPI 凭据与 ACL、Desktop 管理入口、冲突检查、身份及分项健康诊断、保留卷重建/卸载。
- P2：受管本地 Runner 自动安装命令及高级配置包；独立管理/Runner 地址；明确连接模式、协议/能力及身份匹配；SSH 严格主机校验、私有 socket / relay 与重连脚本。VPS 实测未进行。
- P3：定义版本与引用校验、WorkItem/Job 配置快照、角色/模型映射/交付传入节点；fixed/pool、忙/离线/超时、并发限额、撤权复查、固定资源禁止中央工作区回退。Bot 页面有节点选择与完整配置编辑。
- P4：主任务与参与者入口，工作项会话、配置版本、节点与产物状态；分块二进制协议、校验和、幂等 finalize、限额和清理；既有并行协作/等待/转交继续有效。
- P5：PostgreSQL 全量回归、Computer 并发占用修复；测试项目重启持久性验证；冷备份与安全恢复辅助脚本、操作手册。完整跨主机迁移及真实故障矩阵仍需现场验收。

这些是代码与本地验收状态，不代表原计划 A1–A12 已全部通过。新迁移 `f2d21013d006` 在独立测试数据库及 Compose 验证项目中执行；未升级原运行中的 niucai 数据库。未提交代码，避免将大量原有未提交修改合并成混合提交。

## 测试

| 验证 | 结果与证据边界 |
|---|---|
| Linux + 独立 PostgreSQL 全量 pytest | **193 passed**，包含真实行锁、skip-locked、多槽位、等待/取消竞争、恢复和前向迁移；8 条 Alembic 既有配置弃用警告 |
| 后续 Kernel Anywhere 定向测试 | **13 passed**；新增紧急节点撤权保留占用、固定节点离线超时不回退、drain 后已有等待/回复仍可完成 |
| 受影响协作/Pi 定向回归 | **40 passed, 3 skipped**；Windows 跳过 PostgreSQL/Linux 专属场景，相关全量已另在 Linux 跑过 |
| Desktop Vitest | **37 passed**，含新连接模式/relay 包/注入验证 |
| Desktop 构建 | tsc + Vite 成功 |
| Rust 1.98.1 | **4 passed**，可信窗口和传输边界；不等同于 Windows 原生安装点击验收 |
| Ruff / git diff --check | 通过 |
| PowerShell 与 Compose | 受管脚本 AST 解析通过，compose.managed.yml config --quiet 通过；受管生产端口 8080 已被原服务占用，未停掉原服务 |

最初全量 PostgreSQL 回归发现 Computer 等锁之后复用旧 PENDING 身份缓存，导致两个任务同时占用设备；修复为持锁后刷新实际占用任务。另修复父任务恢复到 PENDING 时误撤销独立远程子任务租约。完整 Linux 回归确认修复有效。

原有 memory 测试运行中新增工具的假设与快照合同冲突，改为在创建工作项前配置权限；新增专门用例验证快照不扩权且现行配置仍可撤权。

## 实际容器闭环

独立项目：niucai-anywhere-test，API 127.0.0.1:38184，PostgreSQL、中央 Worker（三槽）、真实 Pi 1.1.0 CLI 和两台 Linux Runner 容器。当前源码挂载在已有固定镜像之上；模型是确定性测试 provider。

实例身份：`28aeea3c-daee-4acf-9b35-0687ea7aa16b`。

连续两次 Workflow：

- `8a5c4512-baa2-4cef-8187-91f76532bac2`
- `6c3fb5b9-4084-44f8-b8c1-49a9ede003ee`

开发节点：`e2c07545-5f37-4182-8736-f76648f78395`；验证节点：`466b79a7-55a0-46a5-99a4-ba8451a5024d`。两个 Bot 配置版本均为 3，两次执行保持绑定和角色；main 配置版本为 1。

路径：main 委派开发 Bot → 显式批准远程包 → 固定节点 bash/Python 生成 **131072 字节**二进制文件 → 分块上传/finalize → Artifact 引用随 Handoff → 验证 Bot 在另一固定节点下载并执行 SHA256 与大小断言 → 生成 test-report.txt → main 汇总。两个 Workflow 都有三工作项、两个实际 Remote Job/attempt 和两次明确审批。

并行/阶段流程：`1424227c-2a23-4e4c-9076-f216f1feb164`（三工作项）及 `0e3df74b-032e-4de0-805d-c1cf05464b9b`（五阶段，包含修订回路）。这些检查与固定资源闭环同时运行，验证独立工作流并行推进。

重启仅涉及独立项目 API、Worker 和两 Runner。恢复后身份相同、四个已完成 Job 的 attempt 不变、历史工作流可读、全部产物 SHA256 一致，分项诊断 execution_ready=true。

本机证据文件（不含 token / API Key / 私钥）：

- build-cache/niucai-anywhere-test/verification.json
- build-cache/niucai-anywhere-test/parallel-verification.json
- build-cache/niucai-anywhere-test/restart-verification.json

## 待验收与限制

| 原计划场景 | 当前证据 |
|---|---|
| A1 / A10 | Linux API/Worker 执行与持久性已验证；Windows 原生 Desktop 安装、关闭/重开点击流程未实测 |
| A2 / A5 / A6 | 两节点实际执行、两次复用、文件交接、并行与阶段转交通过；节点都在本机 Docker |
| A3 / A4 | 未进行真实 VPS / 不同网络拓扑验收；用户要求本轮先完成本地 |
| A7 / A8 / A12 | 服务端自动测试及容器文件交接覆盖；未做真实 SSH 通道故障演练 |
| A9 | 协议/租约/LOST 回归及已完成任务重启持久性通过；全部在途故障注入仍待补 |
| A11 | 交付 drain、冷备份、校验、安全恢复脚本与手册；跨主机迁移/回滚未演练 |

真实模型协作质量、VPS SSH 可达性及现场权限均不从确定性模型结果推断。Docker Desktop 需预先安装；镜像发布链未上线，当前采用源码构建。Bot 配置中较高级字段使用结构化 JSON 编辑；没有宿主机目录默认授权、自动 Git 合并、完整节点 Bot Runtime 或多主机 Worker。
