import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import { ErrorNotice } from "./ui";
import { usePageVisible } from "../lib/visibility";
import { useEffect, useState } from "react";

export function ComputerPreview({ computerId }: { computerId: string }) {
  const visible = usePageVisible();
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    if (!visible) return;
    setNow(Date.now());
    const timer = setInterval(() => setNow(Date.now()), 2000);
    return () => clearInterval(timer);
  }, [visible]);
  const q = useQuery({
    queryKey: ["computer-preview", computerId],
    queryFn: () => api.computerPreview(computerId),
    enabled: visible,
    refetchInterval: visible ? 2000 : false,
    refetchIntervalInBackground: false,
    retry: false,
    gcTime: 0,
  });
  const old = q.data && now - Date.parse(q.data.captured_at) > 10000;
  return (
    <div className="computer-preview" aria-label="只读浏览器画面">
      <ErrorNotice error={q.error} />
      {q.data ? (
        <>
          {(q.error || old || !visible) && (
            <p role="status">
              {!visible
                ? "页面隐藏，已暂停刷新"
                : "以下为上次画面，正在等待更新"}
            </p>
          )}
          <img src={q.data.image} alt="远程浏览器只读画面" draggable={false} />
          <small>
            画面时间：{new Date(q.data.captured_at).toLocaleTimeString()}
          </small>
          <button
            disabled={q.isFetching || !visible}
            onClick={() => void q.refetch()}
          >
            刷新画面
          </button>
        </>
      ) : (
        <p>
          {q.isLoading
            ? "正在获取浏览器画面…"
            : "画面暂不可用，请检查远程浏览器连接。"}
        </p>
      )}
    </div>
  );
}
