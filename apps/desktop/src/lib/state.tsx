import {
  createContext,
  useContext,
  useState,
  useEffect,
  useCallback,
  useRef,
  type ReactNode,
} from "react";
import {
  QueryClient,
  QueryClientProvider,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { listen } from "@tauri-apps/api/event";
import { invoke } from "@tauri-apps/api/core";
import {
  api,
  closeComputer,
  connect,
  disconnect,
  isDemo,
  loadProfile,
  native,
  setDemo,
} from "./api";
import type { ConnectionInput, KernelEvent, Page, Profile } from "./types";

interface State {
  page: Page;
  setPage: (page: Page) => void;
  connected: boolean;
  demo: boolean;
  profile: Profile | null;
  stream: string;
  synchronizing: boolean;
  notice: string;
  notify: (message: string) => void;
  connectTo: (input: ConnectionInput) => Promise<void>;
  disconnectFrom: () => Promise<void>;
  explore: () => void;
}
const Context = createContext<State | null>(null);
export const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: 1, staleTime: 3000, refetchOnWindowFocus: true },
    mutations: { retry: false },
  },
});
export function Provider({ children }: { children: ReactNode }) {
  return (
    <QueryClientProvider client={queryClient}>
      <StateProvider>{children}</StateProvider>
    </QueryClientProvider>
  );
}
function StateProvider({ children }: { children: ReactNode }) {
  const [page, setPage] = useState<Page>("dashboard");
  const [connected, setConnected] = useState(false);
  const [profile, setProfile] = useState<Profile | null>(null);
  const [demo, setDemoState] = useState(false);
  const [stream, setStream] = useState("未连接");
  const [synchronizing, setSynchronizing] = useState(false);
  const [notice, notify] = useState("");
  const cache = useQueryClient();
  const disconnectFrom = useCallback(async () => {
    try {
      await disconnect();
    } finally {
      cache.clear();
      setConnected(false);
      setDemoState(false);
      setStream("未连接");
      setSynchronizing(false);
      setProfile((p) => (p ? { ...p, has_token: false } : null));
    }
  }, [cache]);
  const connectTo = useCallback(
    async (input: ConnectionInput) => {
      const p = await connect(input);
      setDemo(false);
      cache.clear();
      setProfile(p);
      setDemoState(false);
      setConnected(true);
      setStream(native() ? "连接中" : "轮询同步");
      setSynchronizing(native());
      notify("已连接 Kernel");
    },
    [cache],
  );
  function explore() {
    void closeComputer()
      .then(() => {
        setDemo(true);
        cache.clear();
        setDemoState(true);
        setConnected(true);
        setStream("示例");
        setSynchronizing(false);
        setPage("dashboard");
      })
      .catch((e) => notify(String(e)));
  }

  useEffect(() => {
    let active = true;
    loadProfile()
      .then(async (p) => {
        if (!active) return;
        setProfile(p);
        if (p.has_token) {
          try {
            await api.tasks();
            if (active) {
              setConnected(true);
              setStream(native() ? "连接中" : "轮询同步");
              setSynchronizing(native());
            }
          } catch {
            if (active) {
              setConnected(native());
              setStream("等待重连");
              setSynchronizing(native());
              notify("已保存连接，服务器暂不可用，正在尝试恢复连接。");
            }
          }
        }
      })
      .catch((e) => notify(String(e)));
    return () => {
      active = false;
    };
  }, []);
  useEffect(() => {
    if (!connected || demo || !native()) return;
    let stopped = false;
    let healthy = false;
    let revision = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const unlisteners: (() => void)[] = [];
    async function start() {
      const eventOff = await listen<KernelEvent>(
        "kernel-event",
        ({ payload }) => {
          cache.setQueryData<KernelEvent[]>(["events"], (old) =>
            [payload, ...(old || []).filter((e) => e.id !== payload.id)]
              .sort((a, b) => b.id - a.id)
              .slice(0, 100),
          );
          if (!timer)
            timer = setTimeout(() => {
              timer = undefined;
              void cache.invalidateQueries({
                predicate: (q) => q.queryKey[0] !== "events",
              });
            }, 250);
        },
      );
      if (stopped) {
        eventOff();
        return;
      }
      unlisteners.push(eventOff);
      const streamOff = await listen<string>("kernel-stream", ({ payload }) => {
        setStream(payload);
        const available =
          payload === "WebSocket 同步" || payload === "轮询同步";
        if (!available) {
          healthy = false;
          revision += 1;
          setSynchronizing(true);
        } else if (!healthy) {
          healthy = true;
          const current = ++revision;
          setSynchronizing(true);
          void cache.invalidateQueries().finally(() => {
            if (!stopped && revision === current) setSynchronizing(false);
          });
        }
        if (payload === "认证失效") {
          setConnected(false);
          notify("连接凭据已失效，请重新连接。");
        }
      });
      if (stopped) {
        streamOff();
        return;
      }
      unlisteners.push(streamOff);
      const recent = await api.events().catch(() => []);
      if (stopped) return;
      cache.setQueryData<KernelEvent[]>(["events"], (old) =>
        [
          ...new Map(
            [...recent, ...(old || [])].map((event) => [event.id, event]),
          ).values(),
        ]
          .sort((a, b) => b.id - a.id)
          .slice(0, 100),
      );
      await invoke("start_events", { after: recent[0]?.id || 0 });
    }
    void start().catch(() => {
      if (!stopped) {
        setStream("轮询同步");
        setSynchronizing(false);
        void cache.invalidateQueries();
      }
    });
    return () => {
      stopped = true;
      unlisteners.forEach((off) => off());
      if (timer) clearTimeout(timer);
      void invoke("stop_events");
    };
  }, [connected, demo, cache, profile]);
  return (
    <Context.Provider
      value={{
        page,
        setPage,
        connected,
        demo,
        profile,
        stream,
        synchronizing,
        notice,
        notify,
        connectTo,
        disconnectFrom,
        explore,
      }}
    >
      {children}
    </Context.Provider>
  );
}
export function useApp() {
  const value = useContext(Context);
  if (!value) throw new Error("Provider missing");
  return value;
}
export function useData<T>(key: unknown[], fn: () => Promise<T>) {
  const { connected } = useApp();
  return useQuery({
    queryKey: key,
    queryFn: fn,
    enabled: connected,
    refetchInterval: isDemo() ? false : 15000,
  });
}
export function useCommand<T, V>(
  fn: (variables: V) => Promise<T>,
  success?: string,
) {
  const cache = useQueryClient();
  const { notify } = useApp();
  const submitting = useRef(false);
  const mutation = useMutation({
    mutationFn: fn,
    onSuccess: async () => {
      await cache.invalidateQueries();
      if (success) notify(success);
    },
    onError: (e: Error) => notify(e.message || String(e)),
    onSettled: () => {
      submitting.current = false;
    },
  });
  const mutate: typeof mutation.mutate = (...args) => {
    if (submitting.current) return;
    submitting.current = true;
    mutation.mutate(...args);
  };
  return { ...mutation, mutate };
}
