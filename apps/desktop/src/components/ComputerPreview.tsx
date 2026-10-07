import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import { ErrorNotice } from "./ui";

export function ComputerPreview({ computerId }: { computerId: string }) {
  const q = useQuery({
    queryKey: ["computer-preview", computerId],
    queryFn: () => api.computerPreview(computerId),
    refetchInterval: 2000,
    retry: false,
    gcTime: 0,
  });
  return (
    <div className="computer-preview" aria-label="只读浏览器画面">
      <ErrorNotice error={q.error} />
      {q.data ? (
        <>
          {q.error && <p>连接中断，以下为上次画面</p>}
          <img src={q.data.image} alt="远程浏览器只读画面" draggable={false} />
          <small>
            画面时间：{new Date(q.data.captured_at).toLocaleTimeString()}
          </small>
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
