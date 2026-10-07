import { useState } from "react";
import {
  Maximize2,
  Monitor,
  MousePointer2,
  Plus,
  Pause,
  Play,
  CornerUpLeft,
} from "lucide-react";
import {
  api,
  openComputer,
  changeComputerControl,
  toggleComputerFullscreen,
} from "../lib/api";
import { useApp, useCommand, useData } from "../lib/state";
import {
  Badge,
  ConnectionEmpty,
  Empty,
  ErrorNotice,
  Header,
  Loading,
  Modal,
} from "../components/ui";
import { Approvals } from "./Dashboard";
export function ComputerPage() {
  const { connected, demo, profile, notify } = useApp();
  const q = useData(["computers"], api.computers);
  const [selected, setSelected] = useState("");
  const [register, setRegister] = useState(false);
  const [name, setName] = useState("Cloud Computer");
  const computer = q.data?.find((c) => c.id === selected) || q.data?.[0];
  const create = useCommand(() => api.registerComputer(name), "电脑已注册");
  const control = useCommand(async (operation: string) => {
    if (!computer) throw new Error("请选择电脑");
    return changeComputerControl(computer.id, operation, notify);
  }, "控制权已更新");
  const open = useCommand(async () => {
    if (!computer) throw new Error("请选择电脑");
    await openComputer(computer.id);
  });
  return (
    <>
      <Header
        eyebrow="HUMAN + AGENT"
        title="同一台电脑，随时接管。"
        description="先取得控制权，再操作桌面；交还后，Agent 从检查点继续。"
      >
        <button disabled={!connected} onClick={() => setRegister(true)}>
          <Plus size={16} />
          注册电脑
        </button>
      </Header>
      {!connected ? (
        <ConnectionEmpty />
      ) : (
        <>
          <ErrorNotice error={q.error} />
          {q.isLoading ? (
            <Loading />
          ) : computer ? (
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
                <div className="badge-row">
                  <Badge status={computer.status} />
                  <Badge status={computer.control} />
                </div>
              </div>
              <section className="remote-panel">
                <div className="remote-topbar">
                  <span>
                    <Monitor size={15} />
                    {computer.name}
                  </span>
                  <span>
                    {computer.kind === "linux"
                      ? "Linux Computer"
                      : "Android Node"}
                  </span>
                </div>
                <div className="remote-placeholder">
                  <div className="computer-illustration large">
                    <Monitor size={68} />
                  </div>
                  <h2>
                    {computer.control === "HUMAN"
                      ? "控制权在你手中"
                      : computer.control === "PAUSED"
                        ? "电脑已暂停"
                        : "Agent 管理这台电脑"}
                  </h2>
                  <p>
                    {demo
                      ? "当前为示例，不会连接或操作真实设备。"
                      : !profile?.computer_url
                        ? "在 Settings 中配置 KasmVNC 地址，即可打开远程桌面。"
                        : computer.control === "HUMAN"
                          ? "打开独立桌面窗口，鼠标、键盘与剪贴板由远程桌面提供。"
                          : "接管会暂停这台电脑上的 Agent 任务。"}
                  </p>
                  {computer.control === "HUMAN" && (
                    <button
                      className="primary"
                      disabled={
                        open.isPending ||
                        demo ||
                        !profile?.computer_url ||
                        computer.kind !== "linux"
                      }
                      onClick={() => open.mutate(undefined)}
                    >
                      <MousePointer2 size={17} />
                      打开远程桌面
                    </button>
                  )}
                  <p className="browser-context">
                    {String(computer.state.url || "浏览器尚未返回观测结果")}
                  </p>
                </div>
                <div className="remote-controls">
                  <span className="muted">
                    {computer.control === "HUMAN"
                      ? "Agent 已暂停输入"
                      : "状态由 Kernel 管理"}
                  </span>
                  <div className="button-row">
                    {computer.control === "AGENT" && (
                      <button
                        className="primary"
                        disabled={control.isPending}
                        onClick={() => control.mutate("take-control")}
                      >
                        <MousePointer2 size={16} />
                        接管电脑
                      </button>
                    )}
                    {computer.control === "HUMAN" && (
                      <>
                        <button
                          disabled={demo}
                          onClick={() =>
                            void toggleComputerFullscreen().catch((e) =>
                              notify(String(e)),
                            )
                          }
                        >
                          <Maximize2 size={16} />
                          全屏
                        </button>
                        <button
                          className="primary"
                          disabled={control.isPending}
                          onClick={() => control.mutate("hand-back")}
                        >
                          <CornerUpLeft size={16} />
                          交还 Agent
                        </button>
                      </>
                    )}
                    {computer.control === "PAUSED" ? (
                      <button
                        disabled={control.isPending}
                        onClick={() => control.mutate("resume")}
                      >
                        <Play size={16} />
                        恢复
                      </button>
                    ) : (
                      <button
                        disabled={control.isPending}
                        onClick={() => control.mutate("pause")}
                      >
                        <Pause size={16} />
                        暂停
                      </button>
                    )}
                  </div>
                </div>
              </section>
              <section className="panel">
                <div className="section-heading">
                  <h2>待审批动作</h2>
                </div>
                <Approvals />
              </section>
            </>
          ) : (
            <section className="panel">
              <Empty
                title="接入你的第一台 Computer"
                description="先配置云端设备，再在这里注册。注册本身不会创建虚拟机。"
              >
                <button className="primary" onClick={() => setRegister(true)}>
                  注册电脑
                </button>
              </Empty>
            </section>
          )}
        </>
      )}
      {register && (
        <Modal title="注册 Linux Computer" onClose={() => setRegister(false)}>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              create.mutate(undefined, {
                onSuccess: (c) => {
                  setRegister(false);
                  setSelected(c.id);
                },
              });
            }}
          >
            <label>
              电脑名称
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                required
                maxLength={200}
              />
            </label>
            <p className="muted">
              设备执行器和 Chromium CDP 地址在服务器端配置。此处只登记设备。
            </p>
            <button className="primary full-width" disabled={create.isPending}>
              注册
            </button>
          </form>
        </Modal>
      )}
    </>
  );
}
