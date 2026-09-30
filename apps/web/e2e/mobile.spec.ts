import { test, expect, type Page } from "@playwright/test";
const token = "ci-only-mobile-token-00000000000000000000";
async function settings(page: Page) {
  await page.getByRole("button", { name: "更多页面" }).click();
  await page.getByRole("button", { name: "登录与安装设置" }).click();
}
async function login(page: Page) {
  await page.goto("/");
  await settings(page);
  await page.getByLabel("连接 Token").fill(token);
  await page.getByRole("button", { name: "登录 Kernel", exact: true }).click();
  await expect(
    page.getByText("已登录。会话会在七天后过期，重新打开应用可恢复。"),
  ).toBeVisible();
}
async function nav(page: Page, name: string) {
  await page
    .getByRole("navigation", { name: "手机导航" })
    .getByRole("button", { name: new RegExp("^" + name) })
    .click();
}
async function fits(page: Page) {
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth + 1,
    ),
  ).toBeTruthy();
}
test("Android demo navigation, layout and safe takeover", async ({
  page,
}, info) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto("/");
  await page.getByRole("button", { name: "体验示例", exact: true }).click();
  await expect(page.getByText("调研 Agent Harness 的最新进展")).toBeVisible();
  await fits(page);
  await page.screenshot({
    path: `test-results/${info.project.name}-home.png`,
    fullPage: true,
  });
  for (const name of ["对话", "任务", "电脑", "审批", "首页"]) {
    await nav(page, name);
    await expect(page.locator("h1")).toBeVisible();
    await fits(page);
  }
  for (const name of ["文件与下载", "记忆", "登录与安装设置"]) {
    await page.getByRole("button", { name: "更多页面" }).click();
    await page.getByRole("button", { name, exact: true }).click();
    await expect(page.locator("h1")).toBeVisible();
    await fits(page);
  }
  await nav(page, "电脑");
  await page.getByRole("button", { name: "接管电脑", exact: true }).click();
  await expect(page.getByRole("button", { name: "交还 Agent" })).toBeVisible();
  await expect(
    page.getByRole("button", { name: "打开远程桌面" }),
  ).toBeDisabled();
  await page.getByRole("button", { name: "交还 Agent" }).click();
  await expect(
    page.getByRole("button", { name: "接管电脑", exact: true }),
  ).toBeVisible();
  await page.screenshot({
    path: `test-results/${info.project.name}-computer.png`,
    fullPage: true,
  });
  expect(errors).toEqual([]);
});
test("real cookie session, task lifecycle, chat and logout", async ({
  page,
  context,
}) => {
  await login(page);
  const cookies = await context.cookies();
  expect(cookies.find((c) => c.name === "niucai_session")?.httpOnly).toBe(true);
  expect(
    await page.evaluate(() => JSON.stringify({ ...localStorage })),
  ).not.toContain(token);
  await nav(page, "任务");
  await page.getByRole("button", { name: "新建任务", exact: true }).click();
  const name = `Android 任务 ${Date.now()}`;
  await page.getByLabel("任务名称").fill(name);
  await page
    .getByLabel("目标", { exact: true })
    .fill("手机创建任务，关页后状态保持");
  await page
    .getByRole("button", { name: "交给 Agent 执行", exact: true })
    .click();
  await expect(page.locator(".task-detail h3")).toHaveText(name);
  await page
    .locator(".task-detail")
    .getByRole("button", { name: "暂停", exact: true })
    .click();
  await expect(page.locator(".task-detail .badge")).toHaveText("已暂停");
  await page
    .locator(".task-detail")
    .getByRole("button", { name: "恢复", exact: true })
    .click();
  await expect(page.locator(".task-detail .badge")).toHaveText("等待执行");
  await page.reload();
  await nav(page, "任务");
  await expect(page.getByText(name, { exact: true })).toBeVisible();
  await page.getByRole("button", { name: new RegExp(name) }).click();
  await page
    .locator(".task-detail")
    .getByRole("button", { name: "取消", exact: true })
    .click();
  await page.getByRole("button", { name: "确认取消", exact: true }).click();
  await expect(page.locator(".task-detail .badge")).toHaveText("已取消");
  await nav(page, "对话");
  await page.getByLabel("聊天内容").fill("你好，规划一下手机工作流");
  await page.getByRole("button", { name: "发送消息" }).click();
  await expect(
    page.getByText("已记录你的目标。可创建 Task 持续执行。", { exact: true }),
  ).toBeVisible();
  await fits(page);
  await nav(page, "电脑");
  await page.getByRole("button", { name: "接管电脑", exact: true }).click();
  await expect(page.getByRole("button", { name: "交还 Agent" })).toBeVisible();
  await page.getByRole("button", { name: "交还 Agent" }).click();
  await expect(
    page.getByRole("button", { name: "接管电脑", exact: true }),
  ).toBeVisible();
  await settings(page);
  await page.getByRole("button", { name: "退出此手机" }).click();
  await expect(page.getByLabel("连接 Token")).toBeVisible();
  await page.reload();
  await settings(page);
  await expect(page.getByLabel("连接 Token")).toBeVisible();
  expect((await page.request.get("/api/tasks")).status()).toBe(401);
});
test("approval decisions persist in Kernel", async ({ page }, info) => {
  await login(page);
  // Independent approvals for each viewport; fixtures are created through the live API test database.
  const ids = [`${info.project.name}-approve`, `${info.project.name}-deny`];
  await nav(page, "审批");
  for (const [i, id] of ids.entries()) {
    const pending = await (await page.request.get("/api/approvals")).json();
    const row = pending.find((r: { id: string }) => r.id === id);
    expect(row).toBeTruthy();
    const card = page
      .locator(".approval-card")
      .filter({ hasText: id + ".txt" });
    await card
      .getByRole("button", { name: i === 0 ? "批准执行" : "拒绝", exact: true })
      .click();
    await expect(card).toHaveCount(0);
    const actions = await (
      await page.request.get(`/api/tasks/${row.action.task_id}/actions`)
    ).json();
    expect(actions.find((a: { id: string }) => a.id === id).status).toBe(
      i === 0 ? "APPROVED" : "DENIED",
    );
  }
  await page.screenshot({
    path: `test-results/${info.project.name}-approvals.png`,
    fullPage: true,
  });
});
test("installed shell works offline without cached private API data", async ({
  page,
  context,
}) => {
  await page.goto("/");
  await page.evaluate(async () => {
    await navigator.serviceWorker.ready;
  });
  await page.reload();
  await expect
    .poll(() => page.evaluate(() => !!navigator.serviceWorker.controller))
    .toBeTruthy();
  await expect(
    page.getByRole("button", { name: "体验示例", exact: true }),
  ).toBeVisible();
  await context.setOffline(true);
  await page.reload();
  await expect(
    page.getByText("当前离线。服务器任务仍保持原状态，联网后同步。"),
  ).toBeVisible();
  const cached = await page.evaluate(async () => {
    const keys = await caches.keys();
    const urls: string[] = [];
    for (const key of keys)
      for (const req of await (await caches.open(key)).keys())
        urls.push(req.url);
    return urls;
  });
  expect(cached.some((url) => new URL(url).pathname.startsWith("/api/"))).toBe(
    false,
  );
  await context.setOffline(false);
});
