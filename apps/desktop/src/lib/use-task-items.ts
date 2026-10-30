import { useMemo } from "react";
import { api } from "./api";
import { goalsApi } from "./goals";
import { useData, useStale } from "./state";
import { buildTaskItems, pendingCounts } from "./task-view";

/** 侧边栏和任务首页共用同一份列表，保证待处理数量只有一个来源。 */
export function useTaskItems() {
  const tasks = useData(["tasks"], api.tasks);
  const conversations = useData(["conversations"], api.conversations);
  const approvals = useData(["approvals"], api.approvals);
  const goals = useData(["goals"], goalsApi.list);
  const stale = useStale(tasks.isError);
  const items = useMemo(
    () =>
      buildTaskItems({
        tasks: tasks.data ?? [],
        conversations: conversations.data ?? [],
        approvals: approvals.data ?? [],
        goals: goals.data ?? [],
        approvalsUnavailable: approvals.isError,
      }),
    [
      tasks.data,
      conversations.data,
      approvals.data,
      goals.data,
      approvals.isError,
    ],
  );
  const counts = useMemo(() => pendingCounts(items), [items]);
  return {
    items,
    counts,
    stale,
    loading: tasks.isLoading || conversations.isLoading,
    errors: [tasks.error, conversations.error].filter(Boolean),
    goalsFailed: goals.isError,
    approvalsFailed: approvals.isError,
  };
}
