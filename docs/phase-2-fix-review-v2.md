# 远端修复包第二次复核

日期：2026-10-08。对象：最新 `niucai-delivery/niucai-phase2-fix.zip`（161886 字节）。

本次只做本地解包、脚本审查、原始证据分析及 WSL 模拟测试，没有连接或修改远端服务器。

## 判断

两处原有成功误报已修复，可以接受这部分修复交付。恢复后的非终态任务确有推进记录，但已有成功副作用的恢复幂等性仍未验收。浏览器排查获得更强证据，不过文档对阻塞请求的判断与原始日志矛盾，需按实际未响应请求继续排查。

## 包完整性与复跑

- ZIP CRC 检查通过；实际为 87 个文件（回复写 86），ZIP 与解压目录逐文件 SHA256 一致、无额外或缺失文件。数量差异本身不影响本地完整性。
- 在本机 WSL 执行交付的 `ce-runner-mock.sh`：10/10，通过；包括通过摘要后非零退出、空日志、超时、缺失退出码标记。
- 在本机 WSL 执行交付的 `ce-no-replay-mock.sh`：10/10，通过；包括终态恢复返回 500/404/200、查询失败和无法推进。
- 两套测试都使用模拟命令，不访问真实 Docker 或服务。这证明所覆盖的脚本判定逻辑，不能代替实际容器内执行验收。

原始输出：`local-logs/fix-v2-review/ce-runner-mock.sh.log`、`local-logs/fix-v2-review/ce-no-replay-mock.sh.log`。

## 恢复演练：续跑有证据，防重复仍需补场景

`records/nonterminal-resume-stage2-20261008T031301Z.log` 记录恢复后的任务仍处于 WAITING_HUMAN / WAITING_APPROVAL，批准后依次 PENDING、RUNNING、COMPLETED。已有 files.write 动作由 WAITING_APPROVAL 变为 SUCCEEDED，并新增预期的 files.read。该记录支持“恢复后的待审批任务可以继续”。

但是首次写操作在备份前尚未执行，因此尚未覆盖“恢复前已成功的外部副作用不会再次派发”。下一次应先完成经批准的写操作，停在后续真实人工等待，再备份恢复和继续；验证同一会话、已完成动作及调用次数/可观察效果。

`verify-no-replay.sh --expect resumable` 仍要求所有动作行完全相同，不能适用于合法新增步骤或待审批动作变成功的场景；独立 stage2 演练实际上采用另一套校验。建议统一为对先前已成功动作的精确 ID/幂等键/状态检查，单独允许并校验预期的新动作。该项保持部分通过合理。

## 浏览器：getTargetInfo 已响应，页面初始化请求未响应

`records/cdp-probe-v2/C2-prod-playwright-debug.txt` 的实际顺序：

1. 两个 page target 已被附着，分别建立两个 session。
2. 向两个 session 各发送 Page、Runtime、Network、Log、Emulation 初始化请求。
3. browser 级 `Target.getTargetInfo`（id=26）明确收到正常 result。
4. 页面会话的多个请求一直没有响应，随后连接超时。

按请求 ID 与 sessionId 配对，本地解析发现 **20 条未响应请求**，包括两个 session 上的 `Page.enable`、`Page.getFrameTree`、`Runtime.enable`、`Network.enable`、`Runtime.runIfWaitingForDebugger` 等。结果保存于 `local-logs/fix-v2-review/cdp-unanswered.json`。

因此不能继续表述为“Target.getTargetInfo 挂起”。日志也未证明其返回 type=browser 是异常。现有证据支持的表述是“browser 级请求仍能响应，但现有 page target 的初始化未完成”。

建议远端下一轮：

- 对比生产与隔离环境的 targetId、sessionId、browserContextId 和每条请求的匹配结果。
- 用原生 CDP 在生产已存在的 page session 上分别验证 Page.enable / getFrameTree / Runtime.enable，记录精确返回及超时；同时观察 Chrome 页面渲染进程是否存活、阻塞及资源情况。
- 在隔离环境比较已有页面与新建空白页面的初始化；是否关联旧 target、profile 或运行时间仍属于待验证假设。
- 隔离环境目前只证明 connect_over_cdp 成功，继续补打开页面、读取内容、截图的完整验收。
- 不把最后打印的请求等同于卡住的请求，不根据这一日志直接决定更换版本或服务器。

生产浏览器仍未通过验收，保持不切换生产。

## 测试报告保留的小缺口

测试脚本的 Docker run 仍带 `--rm`，退出后才 `docker cp` JUnit；容器通常已自动删除，报告无法按此顺序取回。此外内层 `set -e` 会在 pytest 非零时提前退出，后续输出和 PYTEST_EXIT 标记不会执行。失败现在会正确返回非零，但诊断报告保存尚不完整。

建议移除 `--rm`、由已有 cleanup trap 负责删除；pytest 前暂时关闭 errexit，保存退出码、输出摘要和标记、取回报告后再清理。新增一次真实临时容器测试，验证成功和失败两种情况下 JUnit 都可读取。

本地 Kernel 新增的模型超时及运行时中断能力仍未部署远端，与本交付包的 d0fb10e 基线区分验收。
