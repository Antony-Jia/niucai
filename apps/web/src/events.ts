import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useApp } from "../../desktop/src/lib/state";
import { api } from "../../desktop/src/lib/api";
import type { KernelEvent } from "../../desktop/src/lib/types";
export function useEvents() {
  const { connected, demo, disconnectFrom } = useApp();
  const cache = useQueryClient();
  const [status, setStatus] = useState("未连接");
  useEffect(() => {
    if (!connected || demo) {
      setStatus(demo ? "示例" : "未连接");
      return;
    }
    let stopped = false;
    let ws: WebSocket | undefined;
    let retry: ReturnType<typeof setTimeout> | undefined;
    let invalidate: ReturnType<typeof setTimeout> | undefined;
    let cursor = 0;
    let starting = false;
    function lost() {
      window.dispatchEvent(new Event("connection-lost"));
    }
    function receive(event: KernelEvent) {
      if (!Number.isSafeInteger(event.id) || event.id <= cursor) return;
      cursor = event.id;
      cache.setQueryData<KernelEvent[]>(["events"], (old) =>
        [event, ...(old || []).filter((e) => e.id !== event.id)]
          .sort((a, b) => b.id - a.id)
          .slice(0, 100),
      );
      if (!invalidate)
        invalidate = setTimeout(() => {
          invalidate = undefined;
          void cache.invalidateQueries({
            predicate: (q) => q.queryKey[0] !== "events",
          });
        }, 250);
    }
    async function start() {
      if (
        stopped ||
        starting ||
        document.hidden ||
        ws?.readyState === WebSocket.OPEN
      )
        return;
      starting = true;
      try {
        if (!cursor) {
          const recent = await api.events();
          if (stopped) return;
          cache.setQueryData(["events"], recent);
          cursor = recent[0]?.id || 0;
        }
        const url = new URL("/api/events", location.origin);
        url.protocol = location.protocol === "https:" ? "wss:" : "ws:";
        ws = new WebSocket(url);
        ws.onopen = () => {
          setStatus("实时同步");
          ws?.send(JSON.stringify({ after: cursor }));
          void cache.invalidateQueries();
        };
        ws.onmessage = (e) => {
          try {
            const event = JSON.parse(e.data);
            if (event.id) receive(event);
          } catch {}
        };
        ws.onclose = (e) => {
          lost();
          if (stopped) return;
          if (e.code === 1008) {
            setStatus("登录已过期");
            void disconnectFrom().catch(() => {});
            return;
          }
          setStatus("重连中");
          retry = setTimeout(() => void fallback(), 3000);
        };
        ws.onerror = () => ws?.close();
      } catch {
        lost();
        setStatus("等待网络");
        if (!stopped) retry = setTimeout(() => void fallback(), 3000);
      } finally {
        starting = false;
      }
    }
    async function fallback() {
      try {
        const rows = await fetch(`/api/events?after=${cursor}&limit=200`, {
          credentials: "same-origin",
          cache: "no-store",
        });
        if (rows.status === 401) {
          void disconnectFrom().catch(() => {});
          return;
        }
        if (rows.ok) for (const event of await rows.json()) receive(event);
      } catch {}
      if (!stopped) void start();
    }
    function wake() {
      if (!document.hidden) {
        if (retry) clearTimeout(retry);
        void cache.invalidateQueries();
        void start();
      } else {
        lost();
        ws?.close();
      }
    }
    function expired() {
      lost();
      void disconnectFrom().catch(() => {});
    }
    document.addEventListener("visibilitychange", wake);
    window.addEventListener("online", wake);
    window.addEventListener("session-expired", expired);
    void start();
    return () => {
      stopped = true;
      if (retry) clearTimeout(retry);
      if (invalidate) clearTimeout(invalidate);
      ws?.close();
      document.removeEventListener("visibilitychange", wake);
      window.removeEventListener("online", wake);
      window.removeEventListener("session-expired", expired);
    };
  }, [connected, demo, cache, disconnectFrom]);
  return status;
}
