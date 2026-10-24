# 远程节点多执行引擎（Pi / Claude Code / Codex）

状态：**拟议，未实现**。本文是设计方案，文中的字段、工具参数和命令行选项都不代表现有能力；外部 CLI 的选项与事件格式以实现时锁定版本的 `--help` 与实际输出为准。

目标：让主 Pi（编排者）除了把工作包委派给远程 Pi CLI，也能委派给同一节点上的 Claude Code 或 Codex CLI，结果仍经 Kernel 回到 Desktop / 手机。Kernel 继续拥有状态、审批和执行权，新引擎只是 Node Runner 里可替换的“执行进程”。

相关文档：[远程 Pi 执行节点 V1](remote-pi-nodes.md)、[多 Bot 协作方案](multi-bot-collaboration.md)、[YOLO、对话模式、依赖流程与技能/MCP](yolo-pipelines-extensions.md)。

## 1. 现状与范围

现有链路：主 Pi 调用 `remote_pi_submit` → Kernel 创建 `RemoteJob`（`WAITING_APPROVAL`，YOLO 远程时直接 `QUEUED`）→ 节点 Runner 心跳并 `claim` → 以独立 UID 启动 `pi --mode rpc` → 事件经 `/events` 回传 → `/finish` 写入结果与产物 → 主 Pi 用 `remote_pi_result` 汇总 → Desktop 通过现有 API/WebSocket 展示。

与引擎相关的硬编码点：

| 位置 | 现状 |
|---|---|
| `services/node-runner/runner.py` | `execute()` 固定拼 `pi` 参数、写 RPC `prompt`、按 Pi RPC 帧解析；心跳固定 `executor: "pi"` |
| `control/remote.py` `claim()` | `node.capabilities.get("executor") != "pi"` 时不派发 |
| `storage/db.py` `RemoteJob` | 无引擎字段；`session_id`、`result.executor` 已存在 |
| `pi-runtime/src/main.ts` | `remote_pi_submit` 只有 `prompt / allowed_nodes / artifacts` |
| `services/node-runner/Dockerfile` | 只安装 Pi CLI 1.1.0 |

**本方案包含**：Runner 引擎抽象、Codex 与 Claude Code 两个引擎、引擎能力上报与派发、事件映射、凭据隔离、审批边界、数据库迁移与分阶段落地。

**不包含**（列为后续）：同一节点多执行槽；引擎内逐个工具调用回传 Kernel 审批；跨 attempt 的会话续接；Desktop 直连节点；在 Kernel 本机运行这些 CLI。

## 2. 原则

1. **只经 Kernel**：编排者、Desktop 都不直接 SSH 或调用节点 CLI。工作包、审批、租约、取消、产物都走现有 `RemoteJob` 流程。
2. **引擎是执行进程，不是 Agent 身份**：Bot 仍是 Kernel 中的持久化身份；引擎只决定节点上用哪个 CLI 完成这一个工作包。
3. **默认关闭、显式启用**：节点不配置凭据就不上报该引擎；Bot 默认只允许 `pi`，新引擎需要在 Bot 定义里显式加入。
4. **能力如实上报**：引擎“已安装”不等于“凭据可用”。与现有 Pi 一样，在线只代表进程可启动，模型可用性以真实工作包结果为准。

## 3. Engine 抽象（Node Runner）

`Runner` 保留引擎无关的部分：账本（`active.json`）、租约心跳、取消、进程组清理、输入产物下载、输出产物上传、超时。引擎相关部分抽成接口：

```python
class Engine(Protocol):
    name: str                      # "pi" | "codex" | "claude"

    def probe(self) -> dict:       # 启动时执行一次：版本、是否有凭据、模型
        ...
    def home(self) -> Path:        # 每个引擎独立 HOME / 配置目录
        ...
    def environment(self) -> dict: # 只包含该引擎自己的凭据
        ...
    def command(self, job, workspace, session_dir) -> list[str]:
        ...
    def stdin(self, job, prompt: str) -> bytes:  # Pi 写 RPC 帧，Claude/Codex 直接写提示词
        ...
    def translate(self, frame: dict, state) -> list[Event | Final]:
        ...                        # 原始 JSONL 帧 → 标准事件 / 最终结果
```

要点：

- `execute()` 只按 `job["engine"]` 取引擎（缺省为 `pi`，兼容旧 Kernel），其余流程不变。
- 提示词统一由 Runner 组装（现有的工作区说明、`config_snapshot`、输入产物说明），通过 **stdin** 传入，不放进 argv，避免长度限制以及在 `ps` 中可见。
- 每个引擎使用独立目录 `/data/engine-home/<engine>/`，由引擎 UID 拥有；会话文件放在 `/data/sessions/<engine>/<job-id>`。
- 仍是**每节点一个执行槽**：无论哪个引擎，同一时间只运行一个工作包。
- 现有 `pi_environment()` 会把 `OPENROUTER/ANTHROPIC/OPENAI_API_KEY` 全部传给 Pi，改造时收窄为“只传当前引擎需要的变量”。

### 3.1 引擎调用（拟定，以锁定版本为准）

| 引擎 | 调用 | 关键选项 |
|---|---|---|
| Pi | `pi --mode rpc ...`（现状不变） | 现有 `--no-extensions --no-skills --no-context-files --no-mcp --no-approve --offline` |
| Codex | `codex exec --json -` | `-m <model>`、`--sandbox <profile>`、`--skip-git-repo-check`、`-C <workspace>`、`--output-last-message <file>` 作为最终答案兜底；不交互审批 |
| Claude Code | `claude -p --output-format stream-json --verbose` | `--model <model>`、`--permission-mode <profile>`、`--max-turns <n>`、`--allowedTools` / `--disallowedTools`；只加载 Runner 生成的设置与空 MCP 配置，禁用用户级、项目级设置和 hooks |

引擎不得读取工作区之外的用户配置。Pi 已用 `--no-context-files` 屏蔽上下文文件；Claude Code 和 Codex 会自动读取工作区里的 `CLAUDE.md` / `AGENTS.md`，因此输入产物只能放在 `inputs/` 下，Runner 在启动前确认工作区根目录没有这两个文件，并在设置里关闭项目级指令加载（若锁定版本支持）。

## 4. 事件映射

Kernel 侧事件 `kind` 保持现有集合，不新增类型，Desktop 无需改动解析逻辑。`data` 中新增 `engine` 字段；工具事件仍只上报工具名和是否出错，不上报命令文本或文件内容，与 Pi 的现有粒度一致。

| 标准事件 | Pi RPC（现状） | Codex `exec --json` | Claude Code `stream-json` |
|---|---|---|---|
| `session` `{session_id}` | `response` (`get_state`) 的 `sessionId` | `thread.started` 的 `thread_id` | `system`/`init` 的 `session_id` |
| `assistant` `{text, stop_reason}` | `message_end`（assistant） | `item.completed`，item 类型为 `agent_message` | `assistant` 消息中的 `text` 内容块 |
| `tool_execution_start` `{tool}` | `tool_execution_start` | `item.started`，类型为 `command_execution` / `file_change` / `mcp_tool_call` / `web_search` → 工具名 `shell` / `edit` / `mcp` / `web_search` | `assistant` 消息中的 `tool_use` 块的 `name` |
| `tool_execution_end` `{tool, is_error}` | `tool_execution_end` | 对应 `item.completed`；非零退出码或失败状态 → `is_error` | `user` 消息中的 `tool_result` 块的 `is_error` |
| 最终成功 | `agent_end` 且有答案、`stopReason` 非 error/aborted | `turn.completed`，答案取最后一条 `agent_message`（或 `--output-last-message` 文件） | `result` 帧，`subtype=success` 且 `is_error=false`，答案取 `result` |
| 最终失败 | 无答案或异常结束 | `turn.failed` / `error` | `result` 帧的错误类 `subtype` 或 `is_error=true` |

其它约定：

- `reasoning`、`todo_list` 等推理和计划类帧不上报，只计入调试日志（默认关闭）。
- 未识别的帧类型忽略并计数；完成时如果计数非零，在结果中记录 `unknown_frames`，便于发现 CLI 升级导致的格式漂移。
- 进程退出却没有收到最终帧，按 `FAILED` 处理，错误信息为“<engine> exited before completion”。
- 结果统一为 `{answer, executor, session_id, usage?, files}`：Claude 的 `total_cost_usd` / `num_turns`，以及 Codex `turn.completed` 中的 token 用量，写入 `usage`，供预算统计使用。
- 文本截断规则不变（事件 4000 字符、答案 16000 字符），产物上传完全复用 `upload_files` / `collect_files`。

## 5. 凭据边界

| 项目 | 规则 |
|---|---|
| 存放位置 | 只在节点本机 `.env`，与现有 `NIUCAI_PI_PROVIDER_KEY` 相同；Kernel 不保存、不下发模型凭据，接入向导下载的配置包里留空 |
| 注入方式 | Runner 只给当前引擎进程传入该引擎的变量：Codex 用 `CODEX_API_KEY`（或 `OPENAI_API_KEY`），Claude Code 用 `ANTHROPIC_API_KEY`，可选 `ANTHROPIC_BASE_URL` / Codex 自定义 provider 地址 |
| 节点 token | `NIUCAI_NODE_TOKEN` 永不进入引擎环境；引擎运行在独立 UID 下，无法读取 Runner 进程环境 |
| 订阅登录 | V1 **只支持 API Key**。Claude 订阅 OAuth、ChatGPT 账号登录（`auth.json`）需要把可刷新的登录态长期放在引擎可写的目录里，且用于无人值守自动化前需用户自行确认服务条款，因此列为后续选项，不做默认路径，也不从开发机复制登录目录 |
| 能力上报 | 只有 CLI 可执行且对应凭据变量非空时，才上报 `available: true`；不在心跳中上报凭据内容或其哈希 |
| 重置 | 沿用现有节点凭据重置；模型凭据轮换只需改节点 `.env` 并重建容器 |

## 6. 审批与执行边界

**审批粒度不变：Kernel 审批的是“工作包 + 引擎 + 权限档位”，不是引擎内部的单个工具调用。**

- 工作包创建时把 `engine` 和 `permission_profile` 写入 `config_snapshot`；审批卡片（Desktop / 手机）必须显示这两项。批准后不可更改，重试沿用快照。
- 会话 YOLO（`autonomy.remote`）规则不变：可让工作包跳过人工批准，但仍写 `approve` 事件（`policy=yolo`），并受 `NIUCAI_YOLO_ENABLED` 总开关控制。
- 引擎内部一律非交互运行，无法弹出确认框，所以**容器才是安全边界**：非 root UID、只读根文件系统、tmpfs、独立数据卷、不挂载 Docker socket、不含 Kernel 凭据，沿用现有 Runner Compose 配置。

权限档位（拟定）：

| 档位 | Codex | Claude Code | 用途 |
|---|---|---|---|
| `read` | `--sandbox read-only` | `--permission-mode plan` 或只读工具白名单 | 代码审阅、分析，不写文件 |
| `workspace`（默认） | `--sandbox workspace-write` | `--permission-mode acceptEdits` + 工具白名单（编辑和受限 Bash） | 在工作区内修改并产出文件 |
| `full` | `--sandbox danger-full-access` | `--permission-mode bypassPermissions` | 仅限容器内需要任意命令时使用；需 Bot 定义显式允许 |

待验证：Codex 的 `workspace-write` 依赖内核沙箱（Landlock/seccomp），在无特权 Docker 容器中可能不可用。如果实测不可用，`workspace` 档改为由容器隔离兜底，并在审批卡片上如实标注“引擎内沙箱不可用”，不得假装仍有引擎内隔离。

网络：Claude Code 和 Codex 必须能访问模型 API；Web 搜索、抓取类工具默认禁用（Claude 加入 `--disallowedTools`，Codex 关闭 web search 配置），由权限档位或 Bot 定义显式开启。

后续选项（不在 V1）：Claude Code 的权限提示工具（`--permission-prompt-tool`）可以接一个回调到 Kernel ActionGateway 的 MCP 工具，实现逐个工具调用的审批。这需要节点到 Kernel 的同步审批通道，并且审批期间租约要能保持，代价较大，单独立项。

## 7. Kernel 与工具改动

### 7.1 能力上报（protocol_version 3）

心跳保留 `executor: "pi"`，旧 Kernel 仍能识别；新增 `executors`：

```json
{
  "executor": "pi",
  "protocol_version": 3,
  "executors": {
    "pi":     {"available": true,  "version": "1.1.0", "model": "..."},
    "codex":  {"available": true,  "version": "<pinned>", "model": "...", "profiles": ["read", "workspace"]},
    "claude": {"available": false, "reason": "credential_missing"}
  }
}
```

Kernel 将 protocol 1/2 节点视为 `executors = {"pi": ...}`。`claim()` 中的 `executor != "pi"` 判断改为“`job.engine` 在节点的 `executors` 中，且 `available` 为 true，且请求的权限档位包含在该引擎的 `profiles` 中”。

### 7.2 数据模型

- `remote_jobs.engine`：`String(20)`，`server_default="pi"`，非空。历史数据自动为 `pi`。
- `permission_profile` 放在 `config_snapshot` 中，不加列。
- `session_id` 复用现有列，存放 Claude 的 `session_id` 或 Codex 的 `thread_id`。
- 新增 Alembic 迁移（如 `..._remote_engines.py`），要求 `alembic check` 通过。

### 7.3 Bot 定义

新增 `allowed_engines`，缺省为 `["pi"]`；`max_permission_profile` 缺省为 `workspace`。`authorized()` 在每个边界（提交、派发、心跳）重新检查，规则与现有 `allowed_nodes` 相同，修改策略即可撤销权限。固定资源 Bot（`resource_policy.strategy=fixed`）的规则不变，只能委派给绑定节点上已上报的引擎。

### 7.4 主 Pi 工具

V1 不改工具名，避免牵动 `permitted(..., "remote_pi_submit")`、固定资源规则和上下文编译器：

- `remote_pi_submit` 新增可选参数 `engine`（`pi` | `codex` | `claude`，缺省 `pi`）和 `permission_profile`。
- `remote_pi_nodes` 返回每个节点的 `executors`，编排者据此选择引擎。
- 工具描述补充选择建议：编码、重构、修改测试类任务优先 `codex` / `claude`；需要 Pi 工具链或产物约定的任务用 `pi`。最终由模型决定，Kernel 只校验是否允许。

更名为 `remote_agent_*` 留到所有引擎稳定后，届时保留旧名作为别名。

### 7.5 Desktop / 手机

- 节点页：分引擎显示可用性与原因（未安装 / 缺凭据 / 可用）。
- 任务列表、详情和审批卡片：显示引擎标签和权限档位。
- 接入向导：勾选要安装的引擎和模型名；API Key 只在节点 `.env` 中填写，向导里不出现。
- 事件流和结果展示复用现有组件。

## 8. 镜像

`services/node-runner/Dockerfile` 增加构建参数 `ENGINES`（默认 `pi`），按需安装并**锁定版本**的 `@openai/codex` 和 `@anthropic-ai/claude-code`。不使用 `latest`，升级 CLI 版本时同时更新事件映射的测试夹具。每个引擎使用独立的 UID（如 10001 / 10002 / 10003），引擎之间也不能读取对方的 HOME。

## 9. 迁移计划

| 阶段 | 内容 | 完成标准 |
|---|---|---|
| E0 Runner 重构 | 抽出 `Engine` 接口，只实现 `PiEngine`，行为与现在逐字节一致；收窄环境变量 | `verify-remote-pi.py`、`verify-resource-nodes.py` 全部通过，无行为差异 |
| E1 Kernel 基础 | `remote_jobs.engine` 迁移；protocol 3 能力解析；`claim()` 按引擎派发；Bot `allowed_engines`；API 返回引擎字段；Desktop 显示标签 | 迁移与 `alembic check` 通过；protocol 2 节点只接 `pi` 工作包；越权的引擎请求被拒绝 |
| E2 Codex 引擎 | `CodexEngine`、镜像构建参数、接入向导字段；实测容器内 sandbox 档位 | 夹具单测覆盖成功、失败、取消、超时、未知帧；真实 Codex 在 Docker 节点完成一个带产物的工作包 |
| E3 Claude Code 引擎 | `ClaudeEngine`，禁用用户级设置和 MCP，工具白名单 | 同 E2 |
| E4 编排 | `remote_pi_submit` 的 `engine` / `permission_profile` 参数；`remote_pi_nodes` 返回引擎；审批卡片显示引擎与档位 | 主 Pi 在同一会话中把两个工作包分别派给 `codex` 和 `pi` 并完成汇总 |
| E5 后续 | 会话续接（`claude --resume`、`codex exec resume`）、订阅登录、逐工具审批、多执行槽 | 单独立项 |

进度：E0 的代码已改（`PiEngine` 与 `Runner` 拆分，未知 `engine` 的工作包经账本以 `FAILED` 结案）。验证：`ruff` 检查与格式检查；`PiEngine` 的事件翻译 / stdin / 提示词 / 命令行 / 心跳单元对照；`deploy/compose/remote-pi-demo.ps1` 重新构建镜像后，`verify-remote-pi.py` 四项全部通过（双节点并行与第三任务排队、取消与原节点重试、停机后不重复执行并核对旧 attempt、主 Pi 委派与聚合），节点容器内确认运行的是拆分后的 runner，证据在 `build-cache/remote-pi/verification.json`（gitignored）。模拟模型为确定性服务，不代表真实模型质量。尚未运行 `verify-resource-nodes.py`。

进度（E1）：Kernel 基础已实现并通过测试。`remote_jobs.engine`（迁移 `b4e21024d008`，默认 `pi`）；`POST /api/remote/jobs` 接受 `engine`，心跳接受 protocol 3 的 `executors`；`claim()` 只把工作包派给对应引擎可用的节点，protocol 1/2 节点只视为单一 Pi 执行器；Bot `allowed_engines`（默认 `["pi"]`）在提交（快照策略与当前定义）和 claim/心跳时校验；非 Pi 引擎无可用节点时等待原因为 `ENGINE_NOT_READY`。验证：`ruff`、`alembic upgrade head` + `alembic check`，`test_remote_pi` 新增 5 项，相关套件（remote/resource/kernel_anywhere/multi_bot）通过；全量中 `test_desktop_api::test_artifact_download_and_latest_event_cursor` 与 `test_kernel::test_workspace_containment_and_file_read_limit` 失败，在 E1 之前的基线同样失败，与本改动无关。
E1 有意未做：Runner 仍发送 protocol 2（避免旧 Kernel 拒绝 protocol 3 节点，留到 E2 随第二个引擎一起切换）；Desktop 引擎标签、`permission_profile`、主 Pi 工具的 `engine` 参数留给 E4。

兼容性：

- 新 Kernel + 旧 Runner：只派发 `pi` 工作包，其它引擎的任务在队列里等待有能力的节点，不会误派。
- 旧 Kernel + 新 Runner：旧 Kernel 只读 `executor: "pi"`，不会下发 `engine`，Runner 按 `pi` 执行。
- 回滚：迁移只增加带默认值的列，降级时可保留；已提交的非 `pi` 工作包需要先取消。

## 10. 验证

- **单元测试**：为每个引擎录制锁定版本下的原始 JSONL 夹具（成功、工具报错、模型错误、最大轮次、中途退出），测试 `translate()` 输出的标准事件和最终结果。夹具不得包含凭据。
- **确定性联调**：现有模拟模型服务只提供 OpenAI Chat Completions 接口。要让 Codex 和 Claude 走真实 CLI 链路，需要模拟服务增加 Responses API 和 Anthropic Messages API 的最小实现，再通过 base URL 指向它。在此之前，联调结论只能写“夹具验证”，不能写成真实 CLI 验证。
- **故障注入**：沿用 remote-pi 验证脚本中的取消、租约撤销、Runner 重启（LOST 核对）、超时场景，按引擎各跑一遍。
- **真实模型**：每个引擎至少在一台 Docker 节点上用真实凭据跑通一次，记录到验证文档。真实 VPS 验收单独记录。

## 11. 待决问题

1. Codex `workspace-write` 在无特权容器内是否可用（见第 6 节），决定 `workspace` 档位的实际隔离强度。
2. 预算：Claude 可以用 `--max-turns`（以及版本支持时的费用上限）约束，Codex 只能依赖超时和 token 统计。是否需要在 Kernel 侧按 `usage` 实现跨工作包的硬上限，与多 Bot 方案的预算规则合并处理？
3. 工作区里存在 `AGENTS.md` / `CLAUDE.md` 时，是拒绝启动，还是改名保留？当前倾向拒绝启动，并在结果中说明原因。
4. 是否允许同一节点按引擎各开一个执行槽。V1 不做，需要等资源占用和计费模型明确后再定。
