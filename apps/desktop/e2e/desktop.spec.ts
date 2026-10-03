import { test, expect } from "@playwright/test";
test("desktop demo renders all seven pages and takes control", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto("/");
  await page.getByRole("button", { name: "体验示例", exact: true }).click();
  await expect(
    page.locator(".page-content").getByText("调研 Agent Harness 的最新进展"),
  ).toBeVisible();
  await expect(page.locator(".timeline-item")).toHaveCount(2);
  await page.screenshot({ path: "test-results/dashboard.png", fullPage: true });
  for (const name of [
    "Tasks",
    "Chat",
    "Files",
    "Memory",
    "Settings",
    "Computer",
  ]) {
    await page
      .getByRole("button", { name: new RegExp(`^${name} [1-7]$`) })
      .click();
    await expect(page.locator("h1")).toBeVisible();
  }
  await page.getByRole("button", { name: "接管电脑", exact: true }).click();
  await expect(page.getByText("控制权在你手中")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "打开远程桌面" }),
  ).toBeDisabled();
  await page.getByRole("button", { name: "交还 Agent" }).click();
  await expect(
    page.getByRole("button", { name: "接管电脑", exact: true }),
  ).toBeVisible();
  expect(errors).toEqual([]);
});

test("three-column workspace selects tasks, creates goals and expands the desktop", async ({
  page,
}) => {
  await page.goto("/");
  await page.getByRole("button", { name: "体验示例", exact: true }).click();
  await page.getByRole("button", { name: /^Computer 4$/ }).click();
  const session = page.getByRole("region", { name: "任务与对话" });
  const desktop = page.getByRole("region", { name: "云端电脑面板" });
  await expect(session.getByRole("heading", { level: 1 })).toHaveText(
    "调研 Agent Harness 的最新进展",
  );
  await expect(desktop.getByLabel("示例桌面画面")).toBeVisible();
  const left = await page.locator(".sidebar").boundingBox();
  const middle = await session.boundingBox();
  const right = await desktop.boundingBox();
  expect(left!.x + left!.width).toBeLessThanOrEqual(middle!.x);
  expect(middle!.x + middle!.width).toBeLessThanOrEqual(right!.x + 1);
  await page.screenshot({ path: "test-results/workbench.png", fullPage: true });
  await page
    .locator(".recent-sessions")
    .getByRole("button", { name: /梳理项目架构/ })
    .click();
  await expect(session.getByRole("heading", { level: 1 })).toHaveText(
    "梳理项目架构",
  );
  await page.getByLabel("工作台任务目标").fill("在云端电脑整理项目资料");
  await page.getByRole("button", { name: "创建工作台任务" }).click();
  await expect(session.getByRole("heading", { level: 1 })).toHaveText(
    "在云端电脑整理项目资料",
  );
  await expect(
    page.locator(".recent-sessions").getByText("在云端电脑整理项目资料"),
  ).toBeVisible();
  await desktop.getByRole("button", { name: "Files", exact: true }).click();
  await expect(
    desktop.getByRole("heading", {
      name: "每一次工作，都有留存。",
      exact: true,
    }),
  ).toBeVisible();
  await desktop.getByRole("button", { name: "Computer", exact: true }).click();
  await page.getByRole("button", { name: "展开桌面" }).click();
  await expect(session).toBeHidden();
  await page.getByRole("button", { name: "还原工作台" }).click();
  await expect(session).toBeVisible();
  await session.getByRole("button", { name: "对话", exact: true }).click();
  await expect(session.getByLabel("聊天内容")).toBeVisible();
});
