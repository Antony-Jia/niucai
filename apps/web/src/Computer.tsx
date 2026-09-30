import { useEffect, useRef, useState } from "react";
import { api } from "../../desktop/src/lib/api";
import { useApp, useCommand, useData } from "../../desktop/src/lib/state";
import {
  Badge,
  ConnectionEmpty,
  Empty,
  ErrorNotice,
  Header,
} from "../../desktop/src/components/ui";
export function Computer() {
  const { connected, demo, profile, notify } = useApp();
  const q = useData(["computers"], api.computers);
  const [selected, setSelected] = useState("");
  const computer =
    q.data?.find((c) => c.id === (selected || profile?.computer_id)) ||
    q.data?.[0];
  const [view, setView] = useState(false);
  const viewer = useRef<HTMLDivElement>(null);
  const [fullscreen, setFullscreen] = useState(false);
  useEffect(() => {
    setView(false);
  }, [computer?.id, computer?.control, connected, demo]);
  useEffect(() => {
    function lost() {
      setView(false);
      setFullscreen(false);
      if (document.fullscreenElement) void document.exitFullscreen();
    }
    window.addEventListener("connection-lost", lost);
    return () => window.removeEventListener("connection-lost", lost);
  }, []);
  useEffect(() => {
    function changed() {
      if (!document.fullscreenElement) setFullscreen(false);
    }
    document.addEventListener("fullscreenchange", changed);
    return () => document.removeEventListener("fullscreenchange", changed);
  }, []);
  const control = useCommand(async (operation: string) => {
    setView(false);
    setFullscreen(false);
    if (document.fullscreenElement) await document.exitFullscreen();
    if (!computer) throw new Error("请选择电脑");
    return api.computerControl(computer.id, operation);
  }, "控制权已更新");
  const open = useCommand(async () => {
    if (!computer) throw new Error("请选择电脑");
    const current = await api.computers();
    if (current.find((c) => c.id === computer.id)?.control !== "HUMAN")
      throw new Error("请先接管电脑");
    if (profile?.computer_id !== computer.id)
      throw new Error("服务器尚未给这台电脑配置远程桌面入口");
    setView(true);
  });
  async function full() {
    try {
      if (document.fullscreenElement) {
        await document.exitFullscreen();
        setFullscreen(false);
      } else {
        if (viewer.current?.requestFullscreen)
          await viewer.current.requestFullscreen();
        setFullscreen(true);
      }
    } catch {
      setFullscreen((v) => !v);
    }
  }
  return (
    <>
      <Header
        eyebrow="HUMAN + AGENT"
        title="你的云端电脑"
        description="先接管，再操作；交还后，Agent 继续执行。"
      />
      {!connected ? (
        <ConnectionEmpty />
      ) : (
        <>
          <ErrorNotice error={q.error} />
          {computer ? (
            <>
              <div className="toolbar">
                <select
                  aria-label="选择电脑"
                  value={computer.id}
                  onChange={(e) => setSelected(e.target.value)}
                >
                  {q.data?.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.name}
                    </option>
                  ))}
                </select>
                <Badge status={computer.status} />
                <Badge status={computer.control} />
              </div>
              <section className="panel">
                <h2>{computer.name}</h2>
                <p>
                  {computer.control === "HUMAN"
                    ? "你正在控制；Agent 输入已暂停。"
                    : "电脑控制权由 Kernel 管理。"}
                </p>
                <div className="button-row">
                  {computer.control === "AGENT" && (
                    <button
                      className="primary"
                      disabled={control.isPending}
                      onClick={() => control.mutate("take-control")}
                    >
                      接管电脑
                    </button>
                  )}
                  {computer.control === "HUMAN" && (
                    <>
                      <button
                        className="primary"
                        disabled={control.isPending}
                        onClick={() => control.mutate("hand-back")}
                      >
                        交还 Agent
                      </button>
                      <button
                        disabled={
                          demo ||
                          open.isPending ||
                          profile?.computer_id !== computer.id
                        }
                        onClick={() => open.mutate(undefined)}
                      >
                        打开远程桌面
                      </button>
                    </>
                  )}
                  {computer.control === "PAUSED" ? (
                    <button
                      disabled={control.isPending}
                      onClick={() => control.mutate("resume")}
                    >
                      恢复
                    </button>
                  ) : (
                    <button
                      disabled={control.isPending}
                      onClick={() => control.mutate("pause")}
                    >
                      暂停
                    </button>
                  )}
                </div>
                {demo ? (
                  <p className="muted">示例模式不会操作真实设备。</p>
                ) : profile?.computer_id !== computer.id ? (
                  <p className="muted">
                    服务器尚未配置这台电脑的 KasmVNC
                    入口。设备准备好后按部署文档接入。
                  </p>
                ) : (
                  <p className="muted">
                    触摸、键盘、剪贴板和缩放由远程桌面工具栏提供。切换应用或断线会关闭此视图。
                  </p>
                )}
              </section>
              {view && profile?.computer_url && (
                <div
                  className={`mobile-viewer ${fullscreen ? "fullscreen" : ""}`}
                  ref={viewer}
                >
                  <div className="viewer-tools">
                    <strong>HUMAN · {computer.name}</strong>
                    <button onClick={() => void full()}>全屏 / 退出</button>
                    <button
                      onClick={() => {
                        setView(false);
                        setFullscreen(false);
                        if (document.fullscreenElement)
                          void document.exitFullscreen();
                      }}
                    >
                      关闭
                    </button>
                  </div>
                  <iframe
                    title="远程电脑"
                    src={profile.computer_url}
                    allow="fullscreen; clipboard-read; clipboard-write"
                    allowFullScreen
                  />
                  <button
                    className="primary"
                    onClick={() => control.mutate("hand-back")}
                  >
                    交还 Agent
                  </button>
                </div>
              )}
            </>
          ) : (
            <section className="panel">
              <Empty
                title="尚未接入云端电脑"
                description="可以先在 Chat 与 Tasks 使用纯推理任务。电脑注册和执行器在服务器或 Windows 客户端配置。"
              />
            </section>
          )}
        </>
      )}
    </>
  );
}
