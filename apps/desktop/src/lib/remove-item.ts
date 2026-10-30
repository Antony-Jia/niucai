import { request } from "./api";
import { goalsApi } from "./goals";
import { isRemovable, type TaskItem } from "./task-view";

/**
 * 删除一条已结束的任务。三种来源各走自己的接口；对方已经不存在（404）按成功处理，
 * 这样重复点击或另一台设备先删掉时不会报错。
 */
export async function removeItem(
  item: Pick<TaskItem, "sourceKind" | "sourceId">,
) {
  try {
    if (item.sourceKind === "goal") await goalsApi.remove(item.sourceId);
    else if (item.sourceKind === "task")
      await request(`/api/tasks/${item.sourceId}`, "DELETE");
    else await request(`/api/conversations/${item.sourceId}`, "DELETE");
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    if (!/\b404\b/.test(message)) throw error;
  }
}

/** 批量清除：逐条删除，进行中的会被跳过；返回成功数和失败原因。 */
export async function removeFinished(items: TaskItem[]) {
  let removed = 0;
  const failures: string[] = [];
  for (const item of items.filter(isRemovable)) {
    try {
      await removeItem(item);
      removed += 1;
    } catch (error) {
      failures.push(
        `${item.title}：${error instanceof Error ? error.message : String(error)}`,
      );
    }
  }
  return { removed, failures };
}
