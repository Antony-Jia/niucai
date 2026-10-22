import { useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { native, request } from "../lib/api";
import { useApp } from "../lib/state";

export function KernelManager() {
  const { notify, connectTo, connected, demo } = useApp();
  const [source, setSource] = useState(
    localStorage.getItem("niucai.managed-source") ?? "",
  );
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState("");
  async function run(action: string) {
    setBusy(true);
    try {
      localStorage.setItem("niucai.managed-source", source);
      const output = await invoke<string>("managed_kernel", {
        source,
        action,
        modelKey: key || null,
      });
      setKey("");
      setResult(output);
      if (["install", "start", "rebuild"].includes(action))
        await connectTo({
          baseUrl: "http://127.0.0.1:8080",
          computerUrl: "",
          token: "",
          rememberToken: true,
        });
    } catch (error) {
      notify(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="panel" style={{ padding: 20 }}>
      <h2>本机控制中心</h2>
      <p>
        关闭桌面后服务继续运行；电脑休眠、关机或 Docker 停止会中断本机服务。
      </p>
      {native() ? (
        <>
          <label>
            niucai 源码目录
            <input
              value={source}
              onChange={(e) => setSource(e.target.value)}
              placeholder="D:/Code/niucai"
            />
          </label>
          <p>
            当前使用源码构建，需 Docker Desktop 的 Linux 引擎。独立受管项目使用
            127.0.0.1:8080。
          </p>
          <div className="button-row">
            {[
              ["status", "Docker / 容器状态"],
              ["install", "安装并启动"],
              ["start", "启动"],
              ["stop", "停止"],
              ["rebuild", "重建并保留数据"],
              ["logs", "日志"],
              ["diagnose", "执行就绪诊断"],
              ["uninstall", "卸载容器并保留数据"],
            ].map(([action, label]) => (
              <button
                key={action}
                disabled={busy || !source}
                onClick={() => void run(action)}
              >
                {label}
              </button>
            ))}
          </div>
          <label>
            中央模型 API Key
            <input
              type="password"
              autoComplete="off"
              value={key}
              onChange={(e) => setKey(e.target.value)}
            />
          </label>
          <button
            disabled={busy || !source || !key}
            onClick={() => void run("configure-model")}
          >
            保存到 Windows 安全存储
          </button>
          <p>
            模型 ID 使用源码中的
            models.yaml。保存凭据后重建容器生效；节点模型独立配置。
          </p>
        </>
      ) : (
        <p>本机服务管理需要 Windows Desktop。可连接已有控制中心。</p>
      )}
      {connected && !demo && (
        <button
          disabled={busy}
          onClick={() => {
            setBusy(true);
            void request("/api/kernel/diagnostics")
              .then((value) => setResult(JSON.stringify(value, null, 2)))
              .catch((error) => notify(String(error)))
              .finally(() => setBusy(false));
          }}
        >
          诊断当前控制中心
        </button>
      )}
      {result && (
        <pre style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
          {result}
        </pre>
      )}
    </section>
  );
}
