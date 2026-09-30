import { useEffect, useState } from "react";
import { useApp } from "../../desktop/src/lib/state";
import { Header } from "../../desktop/src/components/ui";
interface InstallPrompt extends Event {
  prompt(): Promise<void>;
  userChoice: Promise<{ outcome: string }>;
}
export function Settings() {
  const { connected, demo, connectTo, disconnectFrom, explore, notify } =
    useApp();
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [install, setInstall] = useState<InstallPrompt | null>(null);
  const [update, setUpdate] = useState<ServiceWorker | null>(null);
  useEffect(() => {
    function offered(e: Event) {
      e.preventDefault();
      setInstall(e as InstallPrompt);
    }
    window.addEventListener("beforeinstallprompt", offered);
    navigator.serviceWorker?.getRegistration().then((r) => {
      if (r?.waiting) setUpdate(r.waiting);
      r?.addEventListener("updatefound", () => {
        const worker = r.installing;
        worker?.addEventListener("statechange", () => {
          if (
            worker.state === "installed" &&
            navigator.serviceWorker.controller
          )
            setUpdate(worker);
        });
      });
    });
    return () => window.removeEventListener("beforeinstallprompt", offered);
  }, []);
  async function login() {
    setBusy(true);
    try {
      await connectTo({
        baseUrl: location.origin,
        computerUrl: "",
        token,
        rememberToken: false,
      });
      setToken("");
    } catch (e) {
      notify(String(e));
    } finally {
      setBusy(false);
    }
  }
  async function notifications() {
    try {
      if (!("Notification" in window)) {
        notify("浏览器暂不支持通知");
        return;
      }
      const permission = await Notification.requestPermission();
      if (permission === "granted") {
        const r = await navigator.serviceWorker?.getRegistration();
        await r?.showNotification("niucai 通知已启用", {
          body: "当前页面在线时，可以收到新审批提醒。",
          icon: "/icons/icon-192.png",
        });
        notify("已启用当前页面在线时的审批通知");
      }
    } catch {
      notify("请在浏览器设置中允许通知");
    }
  }
  return (
    <>
      <Header
        eyebrow="ANDROID CONTROL CENTER"
        title="连接你的 Kernel"
        description="与 Windows 使用同一套任务、状态和审批。"
      />
      <section className="panel">
        <h2>手机登录</h2>
        <p className="muted">当前服务器：{location.host}</p>
        {connected && !demo ? (
          <>
            <p>已登录。会话会在七天后过期，重新打开应用可恢复。</p>
            <button
              className="danger"
              disabled={busy}
              onClick={() => {
                setBusy(true);
                void disconnectFrom()
                  .catch((e) => notify(String(e)))
                  .finally(() => setBusy(false));
              }}
            >
              退出此手机
            </button>
          </>
        ) : (
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void login();
            }}
          >
            <label>
              连接 Token
              <input
                aria-label="连接 Token"
                type="password"
                autoComplete="off"
                autoCapitalize="none"
                spellCheck={false}
                value={token}
                onChange={(e) => setToken(e.target.value)}
                required
                minLength={32}
              />
            </label>
            <p className="muted">
              使用 Kernel 的 NIUCAI_API_TOKEN。Token
              不写入浏览器存储，会换取服务器管理的 HttpOnly 会话。
            </p>
            <button className="primary full-width" disabled={busy}>
              {busy ? "连接中…" : "登录 Kernel"}
            </button>
          </form>
        )}
        <button className="full-width" onClick={explore}>
          体验示例
        </button>
      </section>
      <section className="panel">
        <h2>安装到 Android 主屏幕</h2>
        <p>
          使用 Chrome 打开 HTTPS 地址，选择菜单中的“安装应用”或“添加到主屏幕”。
        </p>
        {install && (
          <button
            className="primary"
            onClick={() =>
              void install
                .prompt()
                .then(() => install.userChoice)
                .then(() => setInstall(null))
            }
          >
            安装 niucai
          </button>
        )}
        {update && (
          <button
            onClick={() => {
              if (confirm("更新会重新加载页面，请先交还远程电脑控制权。")) {
                navigator.serviceWorker.addEventListener(
                  "controllerchange",
                  () => location.reload(),
                  { once: true },
                );
                update.postMessage("activate-update");
              }
            }}
          >
            更新客户端
          </button>
        )}
        <p className="muted">
          离线时保留应用界面；任务和审批需要网络。关闭手机端不会停止处于 Agent
          控制下的服务器任务。
        </p>
      </section>
      <section className="panel">
        <h2>审批提醒</h2>
        <p>
          首页与审批页实时显示待处理事项。可开启本页面在线时的系统通知；后台推送暂未接入。
        </p>
        <button onClick={() => void notifications()}>允许在线审批通知</button>
      </section>
    </>
  );
}
