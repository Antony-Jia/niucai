# GitHub Actions CI

`.github/workflows/kernel.yml` 自动检查 main push 和所有 Pull Request，也可在 Actions → Kernel CI → Run workflow 手动触发。

| Job | 内容 |
|---|---|
| Python checks | Python 3.12，锁定依赖，Ruff lint / format |
| Tests (sqlite) | 本地测试、SQLite 迁移 upgrade/check/downgrade/upgrade |
| Tests (postgres) | pgvector PostgreSQL 17，完整测试及并发/行锁测试，真实 PostgreSQL 迁移检查 |
| Container build and smoke test | Docker build、镜像内迁移、非 root 启动、healthz、认证拒绝、Task 创建与持久化 |
| CI passed | 汇总检查；任何依赖失败、取消或跳过都不会给出成功 |

测试报告是 JUnit XML，Actions 页面的 Artifacts 中可下载，保留 14 天。
同分支的新提交会取消较旧的 CI，避免重复占用 Runner。

不需要 OpenRouter Key、远程设备或仓库 Secrets。模型请求使用 mock，DeepAgents 使用 fake Gateway。
容器检查只启动 API，设备适配默认关闭，测试不访问真实 Computer，也不产生模型费用。
CI 数据库密码与 API Token 是测试专用常量，不能用于正式部署。

权限仅 `contents: read`。此工作流只检查和构建，不发布 Docker 镜像、不部署服务器。
若后续启用合并保护，可把 `CI passed` 设为 Required status check；本次未修改仓库访问或合并规则。
