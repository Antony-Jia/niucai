import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Laptop } from "lucide-react";
import { localRunner, request } from "../lib/api";
import { ALLOW_KEY, AUTO_KEY, store, stored } from "../lib/local-runner";
import { useApp } from "../lib/state";

export const LOCAL_WARNING =
  "本机节点以当前 Windows 用户身份运行 Claude Code / Codex：Bot 能读写自己的工作区和你在白名单中授权的目录，Shell 不是安全沙箱。" +
  "建议本机 Bot 使用“只读”档位并开启人工审批。";

/** 把本机作为节点：Desktop 托管 Windows Runner sidecar，退出 Desktop 时完成当前作业后停止。 */
export function LocalNode({ onChange }: { onChange: () => void }) {
  const { notify } = useApp();
  const [allow, setAllow] = useState(() => stored(ALLOW_KEY));
  const [busy, setBusy] = useState(false);
  const status = useQuery({
    queryKey: ["local-runner"],
    queryFn: localRunner.status,
    refetchInterval: 3000,
  });
  const s = status.data;
  const allowList = allow
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
  const active = !!s && (s.running || s.wanted);

  async function run(action: () => Promise<unknown>) {
    setBusy(true);
    try {
      await action();
      await status.refetch();
      onChange();
    } catch (e) {
      notify(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }
  async function start(code: string | null) {
    store(ALLOW_KEY, allow.trim());
    await localRunner.start(code, allowList);
    store(AUTO_KEY, "1");
  }
  async function add() {
    if (!confirm(`${LOCAL_WARNING}\n\n继续把本机加为节点？`)) return;
    const pairing = await request<{ code: string }>("/api/pairing", "POST", {
      name: "本机 Windows",
      resource_type: "windows_local",
    });
    await start(pairing.code);
  }
  return (
    <section className="panel local-node">
      <div className="section-heading">
        <h2>
          <Laptop size={16} /> 本机节点
        </h2>
        {s && (
          <span className={`badge ${s.running ? "online" : "offline"}`}>
            {s.running ? "运行中" : s.wanted ? "重启中" : "未运行"}
          </span>
        )}
      </div>
      <p className="muted">{LOCAL_WARNING}</p>
      {s && !s.available && (
        <p className="error-text">
          未找到 niucai-runner.exe。开发环境先运行
          services/node-runner/build-windows.ps1。
        </p>
      )}
      <label>
        允许 Bot 使用的本机目录（每行一个绝对路径；留空则只用 Runner 工作区）
        <textarea
          rows={2}
          value={allow}
          disabled={active}
          onChange={(e) => setAllow(e.target.value)}
          placeholder={"D:\\Projects\\demo"}
          spellCheck={false}
        />
      </label>
      <div className="button-row">
        {s && !s.paired && (
          <button
            className="primary"
            disabled={busy || !s.available}
            onClick={() => void run(add)}
          >
            把本机加为节点
          </button>
        )}
        {s?.paired && !active && (
          <>
            <button
              className="primary"
              disabled={busy || !s.available}
              onClick={() => void run(() => start(null))}
            >
              启动本机节点
            </button>
            <button
              disabled={busy}
              onClick={() => {
                if (
                  confirm(
                    "解除本机配对会删除本机节点凭据；Kernel 中的节点需另行吊销。继续？",
                  )
                )
                  void run(localRunner.forget);
              }}
            >
              解除配对
            </button>
          </>
        )}
        {active && (
          <>
            <button
              disabled={busy}
              onClick={() =>
                void run(async () => {
                  store(AUTO_KEY, "");
                  await localRunner.stop("drain");
                })
              }
            >
              停止（完成当前作业后）
            </button>
            <button
              disabled={busy}
              onClick={() =>
                void run(async () => {
                  store(AUTO_KEY, "");
                  await localRunner.stop("now");
                })
              }
            >
              立即停止
            </button>
          </>
        )}
      </div>
      {s?.lastError && (
        <p className="error-text" role="alert">
          {s.lastError}
        </p>
      )}
      {s?.log && (
        <details>
          <summary>Runner 日志</summary>
          <pre className="pairing-command">{s.log}</pre>
          <small className="muted">数据目录：{s.dataDir}</small>
        </details>
      )}
    </section>
  );
}
