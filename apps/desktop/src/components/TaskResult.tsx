import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, downloadArtifact } from "../lib/api";
import { useApp, useCommand, useData } from "../lib/state";
import type { Action, Task } from "../lib/types";
import { ErrorNotice } from "./ui";

export function TaskResult({
  task,
  actions,
}: {
  task: Task;
  actions: Action[];
}) {
  const { notify, demo } = useApp();
  const artifacts = useData(["task-artifacts", task.id], () =>
    api.taskArtifacts(task.id),
  );
  const [previewId, setPreviewId] = useState<string | null>(null);
  const preview = useQuery({
    queryKey: ["artifact-preview", previewId],
    queryFn: () => api.artifactPreview(previewId!),
    enabled: Boolean(previewId) && !demo,
    retry: false,
    gcTime: 0,
  });
  const download = useCommand((id: string) => downloadArtifact(id));
  const terminal = ["COMPLETED", "FAILED", "CANCELLED"].includes(task.status);
  const lastPage = [...actions]
    .reverse()
    .find(
      (a) =>
        a.status === "SUCCEEDED" &&
        typeof a.result.url === "string" &&
        a.spec.type?.toString().startsWith("browser."),
    )?.result.url;
  const failed = [...actions]
    .reverse()
    .find((a) => ["FAILED", "UNKNOWN"].includes(a.status));
  return (
    <section className="task-result" aria-label="任务结果与文件">
      <h4>{terminal ? "执行结果" : "已生成文件"}</h4>
      {typeof task.checkpoint.result === "string" && (
        <p className="preserve">{task.checkpoint.result}</p>
      )}
      {terminal && !task.checkpoint.result && (
        <p className="muted">
          {task.status === "COMPLETED"
            ? "任务已完成，暂无文字摘要。"
            : "任务未完成，已生成的文件仍保留。"}
        </p>
      )}
      {typeof lastPage === "string" && (
        <p className="preserve">最后成功观测页面：{lastPage}</p>
      )}
      {terminal && (
        <p>
          已成功动作：{actions.filter((a) => a.status === "SUCCEEDED").length} /{" "}
          {actions.length}
        </p>
      )}
      {failed && (
        <p>
          {failed.status === "UNKNOWN" ? "需核对的动作" : "失败动作"}：
          {String(failed.spec.type)}（{failed.id.slice(0, 8)}）
        </p>
      )}
      <ErrorNotice error={artifacts.error || download.error} />
      {artifacts.isLoading && <p>正在读取任务文件…</p>}
      {artifacts.data?.length === 0 && (
        <p className="muted">此任务尚无文件。</p>
      )}
      {artifacts.data?.map((a) => (
        <div className="task-artifact" key={a.id}>
          <span>{a.path}</span>
          <div className="button-row">
            {["image/png", "image/jpeg"].includes(a.media_type) && (
              <button disabled={demo} onClick={() => setPreviewId(a.id)}>
                预览截图
              </button>
            )}
            <button
              disabled={demo || download.isPending}
              onClick={() =>
                download.mutate(a.id, {
                  onSuccess: (path) => {
                    if (path) notify("文件已保存到 " + path);
                  },
                })
              }
            >
              下载
            </button>
          </div>
        </div>
      ))}
      {previewId && (
        <div className="artifact-preview">
          <button onClick={() => setPreviewId(null)}>关闭预览</button>
          <ErrorNotice error={preview.error} />
          {preview.isLoading && <p>正在读取截图…</p>}
          {preview.isError && (
            <p>
              截图预览不可用，可通过下载查看；旧版 Kernel 可能尚未提供预览接口。
            </p>
          )}
          {preview.data && <img src={preview.data.image} alt="任务截图预览" />}
        </div>
      )}
    </section>
  );
}
