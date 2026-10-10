# niucai 远程环境安装、验收与排障手册

整理日期：2026-10-10。适用范围：单 Linux 服务器、单 Worker、单 Linux Computer，Windows 客户端通过 SSH 隧道访问。依据当前仓库及 Hermes 历次交付整理，不依赖旧交付 ZIP 才能安装。

本手册中的安装命令已按仓库文件核对，但尚未在一台全新服务器上完整演练。已有环境的联调通过，不等于新服务器从零安装已验收。旧服务器 CentOS 7 的特殊镜像源处理只作为历史经验，不作为新机器的默认配置。

## 1. 开始前固定版本和部署信息

先填写并保存一份部署记录；秘密另行受限保存，不填入记录或 Git。

| 项目 | 需要记录的值 |
| --- | --- |
| 源码 | 仓库地址、完整 Git commit、工作树是否干净 |
| 服务器 | 地址、SSH 用户、独立核实的 SSH 主机指纹 |
| 软件 | Docker Engine / Compose 版本、宿主架构 |
| 镜像 | API/Worker/migrate 的同一 Kernel 镜像、Computer 镜像、镜像 ID；如已推 registry，记录 digest |
| 入口 | API 和桌面的服务器回环端口、Windows 本地转发端口 |
| 存储 | Compose 项目、网络名、每一个真实卷名、备份位置 |
| 业务 | Computer ID、模型配置文件、运行时与 adapter 类型 |
| 凭据 | 只记录保管位置；数据库密码、API Token、模型 Key、VNC 密码分别生成 |

已接收的浏览器截图修复在本地提交 `3230dd5`；Hermes 对应补丁提交为 `95b1daa`。新部署选择包含该修复的完整提交。2026-10-10 的本地原生窗口及任务按钮修复仍有未提交内容，不能认为下载旧提交就包含它们。

安装前确认 Docker 可运行，Compose 支持模板中的 `!override`；以合并配置校验结果为准。核对 CPU 架构和镜像平台，现有交付镜像为 `linux/amd64`。

```bash
docker version
docker compose version
uname -m
free -h
df -h
```

历史服务器约 4 GB 内存曾同时运行生产、隔离桌面并承担构建，遇到明显资源竞争。这个数字不是最低内存保证。构建与运行分开；不要同时在运行服务器反复启动多个构建或桌面实例。

## 2. 连接拓扑和开放端口

```text
Windows ── SSH 22 ── 服务器 127.0.0.1:18080 ── Kernel API
                 └─ 服务器 127.0.0.1:16901 ── desktop-relay ── computer:6901
API / Worker ── computer:9223 ── 同容器 CDP relay ── Chrome 127.0.0.1:9222
```

此模式只需要放行 SSH 入站端口；安装时另需出站访问代码仓库、镜像及依赖源。API、桌面、PostgreSQL 和 CDP 无需开放公网入站端口。移动端公网 HTTPS 接入是另一条部署路径，见 [移动端说明](mobile.md)。

`127.0.0.1` 表示执行命令的那台机器。Windows 使用回环地址的前提是本机已建立 SSH 转发；它不会自行指向 VPS。桌面端口不是 Kernel API 端口。

## 3. 导入源码与配置

在服务器准备独立目录，从仓库检出已确定的完整 commit。先核对 `git status --short`、`git rev-parse HEAD`，再构建。源码 ZIP 必须有校验值和对应 commit；不能用 ZIP 直接覆盖已有 `.env`、卷或运行目录。

以下命令从仓库根目录开始，仅用于全新、无同名环境的单套安装：

```bash
cp deploy/compose/.env.example deploy/compose/.env
chmod 600 deploy/compose/.env
```

通过编辑器填入配置，至少包含以下项；尖括号代表需要替换的值，不能原样启动：

```dotenv
POSTGRES_PASSWORD=<独立数据库密码>
NIUCAI_API_TOKEN=<独立Kernel访问Token>
NIUCAI_OPENAI_API_KEY=<模型服务Key>
NIUCAI_OPENAI_API_BASE_URL=https://api.deepseek.com
NIUCAI_RUNTIME=pi
NIUCAI_ADAPTER=local
NIUCAI_ALLOW_SHELL=false
NIUCAI_BROWSER_CDP_URL=http://computer:9223
NIUCAI_KERNEL_IMAGE=niucai-kernel:<版本标记>
NIUCAI_KERNEL_HOST_PORT=18080
NIUCAI_MODEL_TIMEOUT=120
NIUCAI_RUNTIME_IDLE_TIMEOUT=300
VNC_PW=<独立桌面控制密码>
VNC_VIEW_ONLY_PW=<可选的独立只读密码>
```

注意：

- 远端 Settings 读取 `NIUCAI_` 前缀；不能直接搬用早期本机 `.env` 中无前缀的 `OPENAI_API_KEY` 等字段。
- 模型名在 `services/kernel/models.yaml` 的各角色下配置，不能只设置 `OPENAI_API_MODEL`。使用你的服务端实际可用模型，确认真实模型请求能完成。
- 示例 `.env` 的 adapter 默认 `disabled`，CDP 默认指向 9222；真实 Computer 部署必须改为 `local` 和 9223。
- Token、模型 Key、VNC 密码互不替代。Computer 容器无需模型 Key 或 Kernel Token。
- 文件任务需要 Worker 与 Computer 挂载同一 workspace；Pi 会话不挂入桌面。

## 4. 构建或导入镜像

### 路径 A：从固定源码构建

从仓库根目录执行，版本标记与源码对应：

```bash
REV=$(git rev-parse --short=12 HEAD)
KERNEL_IMAGE="niucai-kernel:$REV"
docker build -f services/kernel/Dockerfile -t "$KERNEL_IMAGE" .
```

把 `.env` 的 `NIUCAI_KERNEL_IMAGE` 改为这个 tag。Kernel Dockerfile 使用 uv 安装锁定 Python 依赖，并构建 Pi 的 Node 运行时；服务器无需另装一套宿主 Python 环境。

网络不稳定时，先确认 GHCR、Docker 镜像源、npm 和 Python 依赖下载条件。`uv.lock` 包含绝对下载 URL，仅更改索引地址不保证替换这些 URL。若必须使用镜像源或构建变体，在独立构建目录记录差异，保留依赖 hash，不把机器专用改写混进正式锁文件。

### 路径 B：其他机器构建，服务器加载

使用同一源码提交构建 Linux/amd64 镜像，导出 tar；传输前后核对 SHA256，再加载。Windows exe 不上传服务器。

```bash
sha256sum -c SHA256SUMS.txt
docker load -i niucai-kernel-linux-amd64.tar
KERNEL_IMAGE='niucai-kernel:替换为交付版本'
docker image inspect "$KERNEL_IMAGE" --format '{{.Id}} {{.Os}}/{{.Architecture}}'
```

校验文件使用 ASCII/UTF-8、LF 行尾。Hermes 的 `95b1daa` 是从已核验基线派生的镜像，不能称为 stock 全量构建；使用旧镜像包时保留构建方式、基线和源码比对记录。

构建退出 137、Killed 或长时间无输出时，先查对应构建进程、资源和内核/cgroup 记录；不能单凭退出码认定 OOM，也不要按全局命令文本批量杀进程。不能把旧镜像跑新测试的结果算作新版本回归。

## 5. 启动服务与注册电脑

确认不存在同名项目、网络和卷，然后执行：

```bash
cd deploy/compose
dc() { docker compose -f docker-compose.yml -f compose.computer.yml "$@"; }
dc config --quiet
dc build computer
dc up -d --no-build postgres computer desktop-relay
dc up --no-build migrate
dc up -d --no-build api worker
dc ps -a
curl -fsS http://127.0.0.1:18080/healthz
```

迁移必须成功；`migrate` 正常退出 0，不要求长期 running。API/Worker/migrate 必须使用同一 Kernel 镜像。只运行一个 Worker。

`dc config --quiet` 不输出展开后的配置；不要把含秘密的完整 `docker compose config` 或 `docker inspect` 发进聊天和公开日志。

模板的 `!override` 用于替换基础 API 端口列表，避免旧 8080 映射仍被追加。不要删除这个标记。桌面 host 端口在模板中固定为 16901，需要变更时审阅最终合并配置。

在 Windows 建立连接后，通过客户端注册 Linux Computer；也可按 [API 文档](api.md) 使用带 Bearer Token 的 `POST /api/computers` 注册。记录返回的 Computer ID，创建浏览器任务时明确绑定该电脑。不要手改数据库状态为 ONLINE。

## 6. Windows 连接和桌面登录

先从服务器控制台或可信渠道独立取得主机指纹，不能把同一次网络扫描所得指纹直接当作可信依据。仓库根目录执行：

```powershell
.\start-remote-tunnel.ps1 -ServerAddress '<服务器地址>' `
  -SshUser '<SSH用户>' -ExpectedFingerprint '<已核实的SHA256指纹>' `
  -TunnelName remote `
  -LocalKernelPort 18080 -RemoteKernelPort 18080 `
  -LocalDesktopPort 16901 -RemoteDesktopPort 16901
```

脚本要求配置可用的 SSH 密钥，并使用 Git for Windows 的 SSH 工具。换服务器必须同时换地址和预期指纹；不要关闭主机身份校验。端口占用时先检查已有隧道，不重复启动。

客户端 Settings：

| 字段 | 值 |
| --- | --- |
| Kernel | `http://127.0.0.1:18080` |
| Computer | `http://127.0.0.1:16901/vnc.html` |
| Kernel Token | 当前环境的 `NIUCAI_API_TOKEN` |

接管电脑后打开交互桌面。现有基础镜像的实际控制账号为 **`kasm_user`（下划线）**，只读账号为 `kasm_viewer`；`/home/kasm-user` 是 home 路径，不是登录用户名。新镜像或既有持久卷的实际账号须核实，不能只凭环境变量推定。

只读列出实际账号（不输出密码 hash）：

```bash
dc exec -T computer sh -c 'cut -d: -f1 /home/kasm-user/.kasmpasswd'
```

桌面登录使用桌面密码。已有 desktop-home 卷可能保留旧账户状态；调整 `.env` 后应验证真实认证结果，不能假定账户已同步，更不能删 home 卷解决认证问题。

人工结束操作后点击“交还 Agent”。电脑处于 HUMAN 时 Worker 不领取其任务；如果任务还处于 PAUSED，交还后再点击“恢复”。仅关闭桌面连接不会交还控制权。

移动端代理若启用，`NIUCAI_COMPUTER_WEB_ID/UPSTREAM/USER/PASSWORD` 必须与实际 Computer 和认证账户一致；Windows 直接桌面能登录，不证明移动端代理配置正确。

## 7. 验收顺序：每一步都留下证据

| 顺序 | 检查 | 通过条件 |
| --- | --- | --- |
| 1 | 容器和迁移 | 迁移退出 0，API 健康，Worker 存活，无循环重启 |
| 2 | API 认证 | `/healthz` 为 200；无 Token 的 `/api/tasks` 为 401；正确 Token 可访问 |
| 3 | CDP | 从 API/Worker 路径连接 `computer:9223`，完成页面导航、读取标题、截图；不是仅 discovery HTTP 200 |
| 4 | 桌面 | HTTP 认证成功，WebSocket 建立，画面可见；人工键鼠操作确实生效 |
| 5 | Chat 转任务 | Chat 本身不自动执行；明确转交后生成绑定正确电脑的任务 |
| 6 | 浏览器任务 | 导航独立 URL、读取目标页面标题、保存并视觉复核截图；任务 COMPLETED，预期动作 SUCCEEDED |
| 7 | 审批 | 拒绝写入后文件不存在；批准后文件内容正确；恢复不代替审批 |
| 8 | 任务控制 | 暂停后不继续执行；恢复能继续；停止后 CANCELLED 且无后续动作增长 |
| 9 | 接管交还 | AGENT → HUMAN → AGENT；接管时 Agent 停止操作；必要时再恢复任务 |
| 10 | 客户端断线 | 断开自己的 SSH 隧道后标记状态待同步；重连后取得服务器权威状态，不重建或重放任务 |
| 11 | 恢复演练 | 独立栈恢复数据库、Pi 会话和文件；审批及动作记录对应，续跑成功 |

首先使用受控的 example.com 页面验收，再测试百度等外部站点。测试任务使用独立文件路径和明确目标 URL，避免复用旧页面造成假通过。

记录 commit、镜像 ID、task/action ID、时间、预期和实际结果、文件 hash 与截图。区分真实服务测试、mock/反例测试、浏览器 UI 测试和 Windows 原生交互测试。容器 healthy、截图大小、任务 COMPLETED 任一单项都不足以证明完整链路通过。

HTTP Basic 200 只证明页面入口认证；WebSocket 探针需要正常浏览器 Origin 和 binary 子协议。探针参数错误不能直接认定服务故障。

## 8. 常见故障速查

| 现象 | 优先检查 | 处理 |
| --- | --- | --- |
| 桌面 HTTP 401 | 账号是否误写 `kasm-user`；桌面密码与当前环境是否一致；是否用了生产凭据连接隔离环境 | 读实际账号，独立验证认证；重连后重新填写，勿用 Kernel Token 登录桌面 |
| 401 页面盖住重连按钮 | Windows 客户端是否包含原生窗口恢复修复 | 更新客户端；原生视口只能覆盖画面区域，恢复按钮留在外部 |
| `current webview is not a WebviewWindow` | 添加子 WebView 后仍使用 WebviewWindow 注入的旧客户端 | 更新客户端；本地主 Webview 接收命令，远端子视图不授予 Kernel 权限 |
| 新任务一直 PENDING | 电脑 HUMAN/PAUSED、Worker 是否运行、其他任务是否占用电脑 | 交还/恢复电脑，刷新状态；暂停任务需另点恢复，不能盲目反复创建任务 |
| 暂停后没有恢复入口 | 电脑仍由人工控制、审批或 UNKNOWN 未处理、旧客户端提示不足 | 先处理等待原因；电脑交还、审批或核对后再恢复 |
| Computer 重启循环、xauth 错误 | 空 desktop-home 卷隐藏基础镜像初始化文件 | 使用包含 home-seed 首次初始化的 entrypoint；保留已有非空 home |
| API/Worker CDP refused | 错用 `computer:9222`，中继不在 Chrome 容器内 | 使用同容器中继 `computer:9223`，检查 `/tmp/cdp-relay.out` |
| discovery 200，随后 WS 失败 | discovery 返回不可达的 loopback WebSocket 地址 | 核对 relay 重写结果，验证实际 WS/页面会话 |
| renderer 为 0、V8 CodeRange 错误 | 物理余量、重复运行栈、Chrome 日志和宿主 commit 情况 | 在维护范围内腾资源并复测；不要把 VmSize 当作实际 RAM，也不要直接改 overcommit |
| 页面能读取，截图一直超时 | 后台标签页不产生合成帧 | 使用已接收修复：首次 8 秒超时后激活同一页，再以 20 秒重试一次 |
| 截到了错误页面 | 多标签页仍选择 `context.pages[0]` | 本轮先导航再截图；多标签页目标选择仍是保留问题 |
| 文件 Permission denied | Worker/Computer UID、共享 GID、目录权限与真实挂载 | 按模板保持共享组及 setgid 权限，不用 chmod 777 代替修复 |
| Shell 中 Python 找不到 niucai | 进入了容器系统 Python，而非 uv 虚拟环境 | 用 `uv run --no-sync python` 或 `/app/.venv/bin/python` |
| Windows 启隧道被 SSH banner 中断 | 使用了未修正的 PowerShell 5 脚本 | 更新脚本；按退出码和唯一主机公钥验证，不忽略指纹不匹配 |
| `sha256sum -c` 找不到文件 | 校验文件 CRLF，或不在归档所在目录执行 | 使用 LF 校验文件，在对应目录校验；不要跳过校验 |

内存诊断的历史勘误：原 mmap 探针缺少 ctypes 的 64 位类型声明，4 GB 长度回绕为 0，EINVAL 被误当内核限制。后续正确探针的 NORESERVE 预留到 6 GB 成功。不能沿用“2 GB 虚拟地址上限”结论。本轮无需修改 overcommit；其他机器仍需按实际数据诊断。

## 9. 备份、恢复与升级

### 备份范围

数据库、Pi 会话、workspace、browser-profile、downloads、desktop-home，以及受限保存的部署配置和凭据。数据库与 Pi 会话必须来自同一静默窗口。镜像也应能按记录的版本重新获取。

先暂停新任务提交和服务写入，等待有界动作结束；处理中断动作时保留 UNKNOWN 和人工核对，不把它改成成功。停止 Worker、API、Computer 后才能采集一致备份。这里只停止应用服务，PostgreSQL 保持运行以导出数据库。

以下是当前单套固定卷名模板的备份示例；在维护窗口执行，不适用于未核实卷名的历史环境。从 `deploy/compose` 执行，沿用第 5 节的 `dc` 函数：

```bash
set -euo pipefail
BACKUP="/opt/niucai-backups/$(date -u +%Y%m%dT%H%M%SZ)"
install -d -m 700 "$BACKUP"
dc stop worker api computer desktop-relay
dc exec -T postgres pg_dump -U niucai -d niucai -Fc > "$BACKUP/postgres.dump"
test -s "$BACKUP/postgres.dump"
KERNEL_IMAGE=$(dc images -q worker)
test -n "$KERNEL_IMAGE"
for vol in niucai_pi_sessions niucai_workspace niucai_browser_profile niucai_downloads niucai_desktop_home; do
  docker volume inspect "$vol" >/dev/null
  docker run --rm --user 0 --entrypoint tar \
    -v "$vol:/source:ro" -v "$BACKUP:/backup" "$KERNEL_IMAGE" \
    -C /source -czf "/backup/$vol.tgz" .
done
(cd "$BACKUP"; sha256sum postgres.dump *.tgz > SHA256SUMS.txt)
dc up -d --no-build computer desktop-relay api worker
```

上述命令未自动备份秘密和 Compose 配置，需另行受限备份并保存版本记录；不要输出 `.env` 内容。任何步骤失败都先保留现场并核查，不能把缺少归档的结果报告为成功；恢复应用运行后重新验收。归档必须另存一份到服务器以外的位置。

### 隔离恢复

1. 校验备份和发布镜像；准备新目录、新项目、新网络、新卷和新端口。
2. 修改模板中所有固定卷/网络名称及桌面端口。仅换 `-p` 不够，原模板会继续挂载 `niucai_*` 生产卷。
3. 只启动恢复 PostgreSQL；将 dump 还原到独立数据库，恢复其他归档到独立卷。保留文件属主与权限，绝不清空或覆盖生产卷。
4. 核对 schema 与目标版本、任务、动作、审批、Pi 会话和附件一致性。启动 API 做只读核查，再允许 Worker 执行。
5. 使用备份时尚未结束的专用任务续跑：一条已成功写入、一条未批准写入；核实审批仍有效、成功记录保留、未执行动作批准后才能继续。
6. 若要证明副作用不重复，在真正适配器入口记录独立调用审计，源栈与恢复栈的审计文件不能随备份复制。Pi 转录条数或数据库只有一行不能证明物理调用次数。

真实备份归档未随历史交付包完整提供，因此不能拿历史包代替自己的恢复演练。备份命令通过不等于恢复已验证。

### 发布和回滚

维护前记录旧 commit、镜像 ID、配置和备份；先在隔离栈验证新版本，再安排生产窗口。API/Worker/migrate 同源，配置变更单独审阅。失败时使用明确的旧镜像和旧配置恢复；若已迁移数据库，先确认旧代码是否兼容新 schema，必要时按事先验证的数据库恢复方案处理。

不执行 `docker compose down -v`，不清空 Pi 转录，不自动批准动作，不通过改数据库状态掩盖服务问题。

## 10. 每次重装最终交付什么

- 脱敏部署清单：commit、镜像、版本、端口、Computer ID、真实卷名和配置差异。
- 可复用源码和脚本：Dockerfile、Compose、启动/健康检查、中继、经过验证的运维脚本。
- 证据：验收表、task/action ID、文件校验、必要截图和失败反例；声明未覆盖的场景。
- 发布附件：镜像归档、SHA256、备份及恢复说明。秘密和用户数据受限保存，镜像和大型日志不直接塞入 Git。

Hermes 历史 `niucai-delivery` 为参考材料，未入库的脚本需要审查后才能作为正式工具。其验收脚本曾错误读取顶层 action.type，导致解析异常；应读取 `action.spec.type`。不能单凭日志中的 PASSED 或 PNG 大小判定成功。

尚保留的事项：多标签页目标选择、独立副作用调用审计、移动端代理凭据对齐及原生 Windows 键鼠/保存对话框验收。不要把旧版总表或 mock 通过当成这些事项已完成。

依据文档：[远端部署](remote-deployment.md)、[截图修复复核](phase-2-browser-fix-review-95b1daa.md)、[恢复证据边界](phase-2-fix-review-v4.md)、[本地真实联调](phase-2-client-integration-results-20261010.md)。
