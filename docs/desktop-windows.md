# Windows Desktop · niucai

Desktop 使用 Tauri 2 + React + TypeScript，优先支持 Windows 10/11 x64。Kernel 仍拥有任务、状态、审批、控制权与模型策略；客户端关闭后，服务器上的 Worker 继续执行。

## 安装

在仓库 **Actions → Desktop CI → 最近一次成功运行 → Artifacts** 下载 `niucai-windows-x64`，解压后选择 NSIS `.exe` 安装包（推荐，当前用户安装）或 `.msi`。安装器会在缺少 WebView2 时下载运行时，因此首次安装需要网络。

当前安装包尚未签名，Windows 可能显示发布者未知。只使用本仓库成功 CI 的构建产物。没有自动更新器，升级时重新安装新版本。

不需要 Python、Node、Rust 或本地 GPU。应用首次打开可以使用明确标记的“体验示例”，此模式不会调用真实模型或设备。

## 连接 Kernel

1. 在服务器先升级 Kernel 数据库：`uv run alembic upgrade head`，然后重启 API 与 Worker。
2. 打开 Settings，填写 Kernel **源站地址**，例如 `https://agent.example.com`，不添加 `/api` 后缀。
3. 填写服务器的 `NIUCAI_API_TOKEN`，至少 32 个字符。OpenRouter 密钥只配置在服务器。
4. 如需远程桌面，填写单台云端电脑的 KasmVNC HTTPS 地址，例如 `https://agent.example.com/computer/`。
5. 点击连接。客户端会先验证认证接口；勾选保存时，Token 存入 Windows 凭据管理器。连接配置文件只保存地址，不保存 Token。

公网只允许 HTTPS/WSS；HTTP 只允许 localhost / 127.0.0.1 / ::1 调试。Kernel REST 请求通过 Rust 原生网络层发送，不需要开放 CORS。请求禁止跨源重定向，Token 不写进 URL、浏览器 localStorage 或远程桌面页面。远程桌面的登录由其独立认证入口完成。

Windows 系统凭据条目的服务名为 `niucai.desktop`，账户名为 Kernel 实例 ID 的哈希（旧版为源站哈希）。Settings 的断开并清除凭据会删除当前连接的保存项。

### 多个 Kernel 连接

客户端可同时保存多个 Kernel，例如本机受管 Kernel（`http://127.0.0.1:8080`）和通过 SSH 隧道访问的服务器（`http://127.0.0.1:18080`），并在两者之间切换。同一时刻只有一个“当前连接”，会话、任务和事件流都属于当前 Kernel；切换会清空前端缓存、重启事件流并关闭远程桌面窗口。

- 连接列表保存在应用配置目录的 `connections.json`（只含名称、地址、实例 ID 与是否保存 Token，不含 Token）。旧版 `connection.json` 首次启动时自动迁入。以 Kernel 实例 ID 为键，同一 Kernel 换地址会更新原条目而不是新增。
- Settings 的“已保存的连接”显示每个连接的状态（在线 / 无法访问 / Token 失效 / 未保存 Token），可切换、删除，或点“添加连接”填写新的名称、地址和 Token。侧边栏在保存了两个及以上连接时显示“切换 Kernel”。
- 只能切换到已把 Token 保存到凭据管理器的连接；未保存的需在表单重新输入。切换时若服务器暂时不可达仍会切换并进入重连；若实例 ID 与保存值不一致则拒绝，防止地址被指向另一个 Kernel。
- 删除连接会同时删除其凭据；“断开并清除凭据”只影响当前连接，其他连接保留。
- 相关原生命令：`list_connections`、`activate_connection`、`remove_connection`、`probe_connections`；均仅主窗口可调用。

## 七个页面

| 页面 | 能力 |
| --- | --- |
| Dashboard | 任务、在线电脑、审批与最近事件 |
| Chat | 持久会话，统一 Model Gateway；消息可显式转成独立 Task |
| Tasks | 创建、筛选、详情、计划、动作、暂停、恢复、重试、取消，UNKNOWN 动作人工核验 |
| Computer | 三栏工作台、最近任务、执行过程、对话、内嵌桌面、接管与交还 |
| Files | 查看任务产物并通过 Windows 保存对话框下载，当前单文件上限 100 MB |
| Memory | 查看、新增语义/经历记忆 |
| Settings | Kernel 地址、Token、KasmVNC 地址、凭据保存与清除 |

`Ctrl+1` 至 `Ctrl+7` 切换页面。事件通过已认证 WebSocket 推送，断线重连按持久事件游标补齐；REST 查询同时每 15 秒刷新状态。

Chat 没有操作工具，也不会自动创建 Task。模型不可用时消息仍持久保存，并显示明确失败。API 进程若在回复过程中重启，未完成消息保留 PENDING；请新建会话继续，避免重复发送同一轮请求。

## 三栏工作台

Computer 页面采用浅色三栏布局：左侧导航与最近任务，中间是所选任务的计划、动作、结果、审批及任务输入，右侧是云端桌面。点击左侧任务即可切换；右侧支持切换 Files、管理电脑和展开桌面。对话页签使用独立的持久会话，不把聊天隐式变成 Task。

Windows 使用独立的 WebView2 子视图将 KasmVNC 页面嵌入右侧；远程页面没有 Kernel IPC 能力或 API Token。调整窗口大小时桌面区域随之调整，打开应用弹窗时暂时隐藏远程子视图。离开工作台、断开连接或交还控制权会关闭子视图。电脑管理页仍保留独立窗口入口。

## Human takeover

在 Computer 点击接管后，Kernel 先取得 HUMAN 控制权并暂停这台电脑相关的任务。随后才能在右侧打开远程桌面。交还或暂停会先关闭客户端的远程窗口，再更新 Kernel 控制权。Kernel Action Gateway 会在 HUMAN 控制期间拒绝 Agent 输入操作。

远程窗口不会获得客户端的 Kernel IPC 权限或 API Token。鼠标、键盘、中文输入、剪贴板与桌面显示由服务器 KasmVNC/WebView2 提供；当前仓库不包含云端桌面镜像，需要之后配置并联调真实设备。关闭窗口不自动交还控制权，请使用交还按钮。

当前只允许 HUMAN 控制期间连接远程桌面。Agent 工作时的实时只读观察尚未接入；示例模式的画面明确标为示例，不能当作真实 Agent 执行画面。

这一版连接一个 Kernel、一个 KasmVNC 桌面地址。Computer 注册不会创建虚拟机；多个电脑可以登记、查看状态与控制权，但远程桌面入口只对应配置的单台电脑。Android 尚未接入。

## Windows 源码开发

安装 Node.js 22、Rust（项目固定 1.98.1，MSVC 工具链）、Visual Studio Build Tools 的 Desktop development with C++ 和 Windows SDK，以及 WebView2。

```powershell
cd apps/desktop
npm ci
npm test
npm run desktop:dev
npm run desktop:build -- --bundles nsis,msi -- --locked
```

`npm run dev` 是仅供 UI 开发的浏览器示例模式；真实连接需要 Tauri 原生客户端。安装包输出在 `src-tauri/target/release/bundle/`。

参考：[Tauri Windows prerequisites](https://v2.tauri.app/start/prerequisites/#windows)、[Windows installer](https://v2.tauri.app/distribute/windows-installer/)。

## CI 与验证范围

Desktop CI 使用 Ubuntu 验证 TypeScript、React 工作流测试与前端构建，使用 Windows 2022 验证 Rust 原生测试，构建 NSIS/MSI 并检查应用能否启动。Kernel CI 同时检查新增会话迁移、SQLite/PostgreSQL 持久化与容器。

UI 测试使用示例数据，Kernel 聊天测试使用可控模型替身；这些不代表真实 OpenRouter 或远程桌面已经联调。服务器准备好后，需验证真实接管/交还、中文输入、剪贴板、下载及客户端重启恢复。
