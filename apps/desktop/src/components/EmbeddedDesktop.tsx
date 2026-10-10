import { useEffect, useRef, useState } from "react";
import { closeComputer, embedComputer } from "../lib/api";
import { useApp } from "../lib/state";
import { ErrorNotice } from "./ui";

export function EmbeddedDesktop({
  computerId,
  onClose,
}: {
  computerId: string;
  onClose: () => void;
}) {
  const element = useRef<HTMLDivElement>(null);
  const { notify } = useApp();
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let stopped = false;
    let failed = false;
    let previous = "";
    let timer: ReturnType<typeof setTimeout> | undefined;
    const update = () => {
      if (timer) clearTimeout(timer);
      timer = setTimeout(() => {
        const rect = element.current?.getBoundingClientRect();
        if (stopped || failed || !rect) return;
        const bounds = {
          x: rect.x,
          y: rect.y,
          width: rect.width,
          height: rect.height,
          visible:
            document.visibilityState !== "hidden" &&
            !document.querySelector(".modal-backdrop, .toast"),
        };
        const key = JSON.stringify(bounds);
        if (key === previous) return;
        previous = key;
        void embedComputer(computerId, bounds).catch((e) => {
          if (!stopped) {
            failed = true;
            void closeComputer().catch(() => {});
            setError(String(e));
            notify(String(e));
          }
        });
      }, 60);
    };
    const resize = new ResizeObserver(update);
    const overlays = new MutationObserver(update);
    if (element.current) resize.observe(element.current);
    overlays.observe(document.body, { childList: true, subtree: true });
    window.addEventListener("resize", update);
    document.addEventListener("visibilitychange", update);
    update();
    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
      resize.disconnect();
      overlays.disconnect();
      window.removeEventListener("resize", update);
      document.removeEventListener("visibilitychange", update);
      void closeComputer().catch(() => {});
    };
  }, [computerId, notify, attempt]);
  return (
    <div className="embedded-desktop" aria-label="远程桌面连接区域">
      <div className="embedded-desktop-toolbar">
        <span>
          {error ? "桌面连接失败；控制权仍由 Kernel 管理。" : "云端桌面连接"}
        </span>
        {error && <ErrorNotice error={error} />}
        <p>
          若登录失败或画面无响应，可重新连接，或关闭桌面后使用上方“交还 Agent”。
        </p>
        <div className="button-row">
          <button
            onClick={() => {
              setError("");
              setAttempt((n) => n + 1);
            }}
          >
            重新连接桌面
          </button>
          <button onClick={onClose}>关闭桌面连接</button>
        </div>
      </div>
      <div
        className="embedded-desktop-viewport"
        ref={element}
        aria-label="远程桌面画面"
      >
        正在连接云端桌面…
      </div>
    </div>
  );
}
