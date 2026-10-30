import { useEffect, useRef } from "react";
import { localRunner, native } from "./api";

export const ALLOW_KEY = "niucai.localRunner.workdirAllow";
export const AUTO_KEY = "niucai.localRunner.autostart";

export function stored(key: string) {
  try {
    return localStorage.getItem(key) ?? "";
  } catch {
    return "";
  }
}
export function store(key: string, value: string) {
  try {
    if (value) localStorage.setItem(key, value);
    else localStorage.removeItem(key);
  } catch {
    // 存储不可用时只是不记住设置。
  }
}

/**
 * 上次退出前在运行的本机节点，下次打开 Desktop 时自动拉起。
 * 挂在应用根部而不是“设备”页：local-codex / local-claude 这类绑定在本机节点上的助手，
 * 不能因为用户没有打开设置页就一直显示“不可用”。
 */
export function useLocalRunnerAutostart(
  enabled: boolean,
  onError: (message: string) => void,
) {
  const tried = useRef(false);
  useEffect(() => {
    if (!enabled || tried.current || !native() || !stored(AUTO_KEY)) return;
    tried.current = true;
    void (async () => {
      try {
        const status = await localRunner.status();
        if (!status.paired || status.running || status.wanted) return;
        const allow = stored(ALLOW_KEY)
          .split(/\r?\n/)
          .map((line) => line.trim())
          .filter(Boolean);
        await localRunner.start(null, allow);
      } catch (e) {
        onError(
          `本机节点自动启动失败：${e instanceof Error ? e.message : String(e)}`,
        );
      }
    })();
  }, [enabled, onError]);
}
