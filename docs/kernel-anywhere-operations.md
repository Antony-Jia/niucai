# 本机 Kernel、Runner 与 VPS 操作手册

日期：2026-10-13。使用当前源码构建。Docker Desktop 和 SSH 是前置工具，不自动安装系统软件。

## Windows 本机

在 Desktop 设置中填写源码目录，例如 D:/Code/niucai，使用“安装并启动”。也可在 PowerShell 执行：

```powershell
./deploy/compose/managed-kernel.ps1 -Action install
./deploy/compose/managed-kernel.ps1 -Action diagnose
./deploy/compose/managed-kernel.ps1 -Action logs
```

受管入口固定 http://127.0.0.1:8080。遇到 PORT_CONFLICT 不自动停其他项目；当前开发机原 niucai 项目占用 8080，应另行由用户决定旧服务的处理。验证环境使用独立 38184。中央模型 ID 在 services/kernel/models.yaml；设置页的模型 Key 保存后“重建并保留数据”生效。

资源页面创建“本地 Docker / 受管 Compose 网络”，地址 http://api:8080。创建后填节点模型 Key，自动安装到 niucai-managed 网络；也可下载配置包自行部署。自动安装需要已启动的本机受管 Kernel。模型配置在节点独立保存，完成安装不等于可执行：必须观察真实心跳、Pi 状态和一次模型工作包结果。日志及后续生命周期可用：

```powershell
./deploy/compose/managed-runner.ps1 -NodeId <node-guid> -Action logs
./deploy/compose/managed-runner.ps1 -NodeId <node-guid> -Action stop
./deploy/compose/managed-runner.ps1 -NodeId <node-guid> -Action start
./deploy/compose/managed-runner.ps1 -NodeId <node-guid> -Action rebuild
./deploy/compose/managed-runner.ps1 -NodeId <node-guid> -Action uninstall
```

这些操作保留数据卷。没有 down -v、Docker socket 或任意宿主机工作区授权。Windows 私有凭据目录位于 %LOCALAPPDATA%/niucai/，DPAPI 加 ACL；手动导出 ZIP 本身含节点 token，只能放在私有位置。

## VPS SSH 接入

已知后续验收地址 101.126.159.10，尚未连接。先在系统 SSH config 中设置用户、端口与 IdentityFile/agent，并人工核验主机指纹，随后用别名操作；不要把私钥或模型 Key 发到聊天。

在 VPS 创建 `~/.niucai-tunnel` 并 chmod 700。选择“远程 VPS / SSH 反向隧道”，Runner 地址 http://127.0.0.1:18080，下载包。在 .env 的 NIUCAI_TUNNEL_DIRECTORY 填该目录绝对路径，准备匹配源码、节点模型配置，再启动：

```powershell
./deploy/compose/kernel-tunnel.ps1 -SshHost niucai-vps -RemoteSocket /home/<user>/.niucai-tunnel/kernel.sock
```

```sh
docker compose -f compose.yml up -d --build
docker compose -f compose.yml logs --tail 100 runner relay
docker compose -f compose.yml exec -T runner /opt/runner/bin/python -c 'import urllib.request;print(urllib.request.urlopen("http://127.0.0.1:18080/healthz").status)'
```

最后一步是在 Runner 自己的命名空间验证，不由 Desktop 的 API 200 代替。隧道停止应看到 KERNEL_UNREACHABLE，lease 失效工作保持 LOST，恢复后 Runner 核对原进程退出；不能把 LOST 工作自动转派。

如果 sshd 禁止反向/Unix socket 转发，先建立组织允许的私网或 HTTPS，切换明确的连接模式。Kernel 部署到 VPS 时，Desktop 管理地址与本地 Runner 地址分别更新；改 URL 不会迁移历史。

## Bot 与主任务

Bot 表单保留职责、工具和完整配置编辑。示例（将 ID 换成真实节点）：

```json
{
  "description": "开发并提交文件证据",
  "model_roles": {"executor": "executor"},
  "allowed_nodes": ["local-node-guid"],
  "resource_policy": {
    "strategy": "fixed",
    "fixed_node_id": "local-node-guid",
    "workspace": "managed",
    "max_concurrency": 1,
    "offline_wait_seconds": 900,
    "required_capabilities": ["artifact-v2"]
  },
  "output_contract": {"require_text": true, "min_files": 1, "description": "源文件与测试报告"}
}
```

节点 CLI 仍是自主工作区执行器，故远程工作 Bot 必须显式配置现有要求的 `tools=["*"]`，且每个工作包人工审批。固定节点不把中央推理迁过去。第二个 Bot 固定另一节点，同样要求至少一个报告文件。协作看板创建主任务并选择两 Bot，查看独立会话、节点、配置版本、等待原因、产物 hash 与最终工作流状态。

## 独立本地验收

```powershell
pwsh ./deploy/compose/remote-pi-demo.ps1 -SkipBuild -MultiBot -Project niucai-anywhere-test -ApiPort 38184
docker compose --env-file build-cache/niucai-anywhere-test/.env -p niucai-anywhere-test -f deploy/compose/compose.remote-pi.yml -f deploy/compose/compose.anywhere-test.yml up -d --wait model
uv run --project services/kernel python deploy/compose/verify-kernel-anywhere.py
```

全新机器先去掉 -SkipBuild 构建真实 Pi 镜像；本次使用已有固定 Pi 1.1.0 镜像加当前源码挂载。验证脚本在独立项目内显式审批自己的工作包，只用于确定性测试模型，不验证生产模型质量。证据保存 build-cache/niucai-anywhere-test/verification.json，不包含密钥。

## 冷备份、恢复与回滚

本机受管 Kernel 使用 `kernel-backup.ps1 -Destination <新的私有备份目录>`。在途工作未收敛时返回阻断原因并保持 drain；完成后源 API/Worker 保持停止。工作区、Pi 与数据库都有 manifest SHA256。

将备份通过受保护通道传到新机器；先构建 niucai-kernel:kernel-anywhere-v2 镜像。运行 `kernel-restore.py <目录> --source-stopped`，验证并恢复新的工作区/Pi 卷，按脚本给出的命令启动仅 PostgreSQL、执行 pg_restore。重新配置目标机安全凭据后启动迁移/API/Worker，检查实例 ID、历史、文件 hash、会话和节点重连，再在目标 POST /api/kernel/admission/resume。源实例继续停机。

需要回滚时先 drain/停目标写入，用完整备份恢复旧软件环境，再恢复单一源实例准入。不能通过迁移 downgrade 或同时启动两边调度。此轮未执行跨主机恢复演练。
