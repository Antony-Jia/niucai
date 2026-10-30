import { ArrowLeft } from "lucide-react";
import { api } from "../lib/api";
import { useApp, useData } from "../lib/state";
import { ConnectionEmpty, ErrorNotice, Loading } from "../components/ui";
import { TaskDetail } from "./Tasks";

/**
 * 没有所属会话的独立任务（旧版直接创建的任务）的详情页。
 * 复用任务详情：进度、结果、审批与可用操作都来自该任务自己的数据，不借道会话接口。
 */
export function StandaloneTask({ taskId }: { taskId: string | null }) {
  const { connected, setPage, synchronizing } = useApp();
  const tasks = useData(["tasks"], api.tasks);
  const task = tasks.data?.find((t) => t.id === taskId);
  if (!connected)
    return (
      <>
        <h1>任务详情</h1>
        <ConnectionEmpty />
      </>
    );
  return (
    <div className="standalone-task">
      <div className="detail-bar">
        <button onClick={() => setPage("home")}>
          <ArrowLeft size={14} />
          返回任务
        </button>
      </div>
      <ErrorNotice error={tasks.error} />
      {tasks.isLoading ? (
        <Loading />
      ) : task ? (
        <TaskDetail task={task} stale={tasks.isError || synchronizing} />
      ) : (
        <p className="quiet-note" role="status">
          {tasks.isError
            ? "暂时无法读取这条任务，状态待同步。"
            : "找不到这条任务，它可能已被删除。"}
        </p>
      )}
    </div>
  );
}
