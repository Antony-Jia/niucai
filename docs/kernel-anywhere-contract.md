# Kernel Anywhere v2 合同与兼容矩阵

日期：2026-10-13。代码合同已落地；现场验收状态见 `kernel-anywhere-validation-20261013.md`。

## 基线与迁移

实施开始时，工作区已有未提交的协作、统一会话、远程 Pi 与资源向导实现，包括 `c9d21011d003`、`d0d21011d004`、`e1d21011d005`。本次保留这些改动，前向迁移为 `f2d21013d006`，不执行生产数据库迁移，不创建包含原有未提交改动的混合提交。

新增表：`kernel_instance`、`worker_health`、`artifact_uploads`。新增 JSON：WorkItem.definition_snapshot、RemoteJob.config_snapshot / wait_state、RemoteNode.connection。历史工作项没有快照时采用明确的兼容路径；新工作项固化创建时的定义。历史 Agent 缺省版本为 1；保存时服务器递增版本，客户端不能自定版本。

`kernel_instance_id` 在数据库首次初始化后持久保存；备份恢复保留身份，新数据库生成新身份。Desktop 安全存储按实例区分凭据，连接更新清空查询缓存。Runner 在独立数据卷内保存期待的实例身份，拒绝对另一实例回传旧 ledger 或领取工作。

## API

| 管理合同 | 行为 |
|---|---|
| GET /api/kernel/identity | 返回实例 ID 与协议版本 2；管理鉴权 |
| GET /api/kernel/diagnostics | 单独报告迁移、Worker 心跳、中央 executor 模型解析和凭据是否配置；不返回密钥 |
| POST /api/kernel/admission/drain | 阻止新任务/工作包/委派；既有等待和结果回传仍可收敛；返回在途任务与未确认执行 |
| POST /api/kernel/admission/resume | 人工核验迁移目标后恢复新准入；不能替代旧实例停止写入 |
| POST/PATCH /api/agents | 校验 Node、Computer、委派对象和模型角色引用；离线节点允许保存，状态仍待验证 |
| POST /api/workflows | participant_agent_ids 限制参加者；output_requirements 保存最终交付说明 |
| POST /api/remote/nodes/{id}/revoke | 紧急禁用，运行工作转 CANCELLING，保留占用直至 Runner 确认退出；普通停用仍要求空闲 |

连接配置分别保存 `management_url`、`runner_url`、`mode`、`kernel_instance_id`，不从 resource_type 推断地址。

| mode | 验证与拓扑 |
|---|---|
| compose | Runner 使用 api 服务名，显式加入 niucai-managed 网络 |
| private_network | HTTP 必须使用明确私有 IP；可达性仍由 Runner 实测 |
| https | 公网地址强制 HTTPS |
| ssh_reverse | HTTP 仅允许 127.0.0.1；私有 SSH Unix socket 转至 relay 内 18080，Runner 共享 relay 网络 |
| 无 connection | 历史兼容模式；不宣称通道已配置或版本已验证 |

SSH 实现采用远端私有 Unix socket，而非尝试让桥接容器直接访问 VPS 宿主机回环 TCP。relay 的 127.0.0.1:18080 仅对其共享命名空间内的 Runner 开放，无公网发布、无默认 host network。保留 TCP 反向转发作为已配置 host-network 环境的手动备选。`kernel-tunnel.ps1` 要求预先核验 known_hosts、系统 SSH agent/私钥引用，严格主机密钥检查，断线重连；禁止转发的现场需先提供私网或 HTTPS。

## Bot 策略

`resource_policy` 保存 strategy、fixed_node_id、required_capabilities、managed workspace、max_concurrency、offline_wait_seconds。fixed 必须属于 allowed_nodes（null 保留兼容语义）；pool 仅在授权列表内领取；[] 禁止远程执行。fixed Bot 的工作区文件与 Shell 操作必须经过远程审批工作包，不回退中央 Shell。

每节点一槽。Bot 并发槽限制在远程派发边界执行。NODE_BUSY / NODE_OFFLINE / PI_NOT_READY 可诊断；离线超时产生可恢复 FAILED / NODE_OFFLINE_TIMEOUT，重试仍需审批。LOST 不自动释放或换节点。配置中的 max_concurrency 不表示整个中央 Bot Runtime 被迁到节点，也不构成文件隔离保证。

快照包含角色规则、模型角色映射、节点范围、交付合同和定义版本，实际随工作包传入 Runner。派发、心跳和结果提交复查当前 Bot 权限；撤权阻止新动作。远程 V1 无细粒度工具桥，因此保留现有 `tools=["*"]` 与继承授权要求，不用提示词模拟工具安全限制。模型凭据不自动转发至节点。

`output_contract` 支持 require_text、min_files 和描述。文件最低数量由服务端检查自身产物，输入引用不计入新交付；不满足时任务和工作项失败，不把某一个 Bot 成功当作主流程成功。描述类要求仍需要模型/人工审查。

## 二进制产物协议

节点鉴权路径前缀 `/api/remote/runner/{node}/jobs/{job}`：

1. POST /uploads：attempt_id、安全 basename、size、SHA256、media_type；返回 upload ID、received、chunk_bytes。
2. PUT /uploads/{id}?attempt_id=...&offset=...：最大 1 MiB 二进制块；严格顺序，重复相同块可重放，不同内容或 offset gap 拒绝。
3. POST /uploads/{id}/finalize：完整大小和流式 SHA256 校验，发布不可变 Artifact；重复 finalize 返回同一引用。
4. GET /artifacts/{id}?attempt_id=...：只允许当前工作包的 approved input_artifacts；流式下载，Runner 校验内容哈希，不共享宿主机路径。

默认单文件 20 MiB、工作项 100 MiB、每 attempt 16 文件。服务端设置为 NIUCAI_ARTIFACT_MAX_FILE_BYTES / NIUCAI_ARTIFACT_MAX_ITEM_BYTES；Runner 当前硬上限维持一期默认值。未完成上传定期清理；旧 attempt、撤权、lease 失效或外工作包访问拒绝。老 Runner 的小文本回传保留兼容；新 Runner 声明 artifact-v2 / managed-workspace 能力。文件不自动解压，符号链接不收集，inputs 不重复上传。

| 组合 | 支持 |
|---|---|
| v2 Kernel + v2 Runner | 身份核验、角色快照、二进制传输 |
| v2 Kernel + 未上报协议的历史 Runner | 基础工作包兼容；新二进制能力不视为已验证 |
| 其他协议版本 | 心跳可诊断，禁止领取 |
| v2 Desktop + 历史 Kernel | 显式要求升级以获取实例身份；不静默混用控制中心 |

## 恢复与部署边界

`compose.managed.yml` 仅管理带 com.niucai.managed 标签的项目和卷。Desktop 管理脚本以 Windows DPAPI 保存凭据，端口冲突拒绝启动，普通重建/卸载保留卷；关闭 UI 不触发容器停机。使用当前源码构建，未假定尚未发布的镜像存在。Docker Desktop 需预先安装并切换 Linux 引擎。

冷备份必须先 drain、核对执行退出、停止 API/Worker 写入，再备份 PostgreSQL、workspace（含 Artifact）和 Pi 卷；节点卷另外备份。新主机需重新安全配置凭据，DPAPI 文件不能直接跨 Windows 用户/VPS 解密。恢复脚本拒绝覆盖旧卷，校验 manifest 和 archive 路径，源实例停止为显式前提。数据库恢复步骤在启动 API/Worker 前执行。回滚用备份和旧软件，禁止破坏性 downgrade 与双活。
