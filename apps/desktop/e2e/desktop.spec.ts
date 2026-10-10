import { test, expect } from "@playwright/test";
test("desktop demo renders unified pages and takes control", async ({
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
  for (const name of ["Files", "Memory", "Settings", "会话"]) {
    await page
      .getByRole("button", { name: new RegExp(`^${name} [1-5]$`) })
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
  await page.getByRole("button", { name: /^会话 2$/ }).click();
  const session = page.getByRole("region", { name: "任务与对话" });
  const desktop = page.getByRole("region", { name: "云端电脑面板" });
  await expect(session.getByRole("heading", { level: 1 })).toHaveText(
    "开始一个新会话",
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
  await session.locator(".conversation-execution summary").click();
  const result = session.getByRole("region", { name: "任务结果与文件" });
  await expect(
    result.getByText("已整理结果，文件保存在 workspace。"),
  ).toBeVisible();
  await expect(result.getByText("research/architecture.md")).toBeVisible();
  await expect(
    result.getByRole("button", { name: "下载", exact: true }),
  ).toBeDisabled();
  await page.screenshot({
    path: "test-results/task-result.png",
    fullPage: true,
  });
  await session.getByRole("button", { name: "新建会话" }).click();
  await page.getByLabel("聊天内容").fill("在云端电脑整理项目资料");
  await page.getByRole("button", { name: "发送消息" }).click();
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
  await expect(session.getByLabel("聊天内容")).toBeVisible();
});

test("workspace fits the minimum Windows window and expands across its full width", async ({
  page,
}) => {
  await page.setViewportSize({ width: 900, height: 650 });
  await page.goto("/");
  await page.getByRole("button", { name: "体验示例", exact: true }).click();
  await page.getByRole("button", { name: /^会话 2$/ }).click();
  const desktop = page.getByRole("region", { name: "云端电脑面板" });
  const initial = await desktop.boundingBox();
  expect(initial!.x + initial!.width).toBeLessThanOrEqual(901);
  await page.getByRole("button", { name: "展开桌面" }).click();
  const expanded = await desktop.boundingBox();
  expect(expanded!.width).toBeGreaterThan(700);
  await expect(
    desktop.getByRole("button", { name: "接管电脑", exact: true }),
  ).toBeVisible();
});

test("minimum workspace shows Kernel progress and separates approval from resume", async ({
  page,
}) => {
  await page.setViewportSize({ width: 900, height: 650 });
  await page.goto("/");
  await page.getByRole("button", { name: "体验示例", exact: true }).click();
  await page.getByRole("button", { name: /^会话 2$/ }).click();
  await page
    .locator(".recent-sessions")
    .getByRole("button", { name: /保存研究结论/ })
    .click();
  // Feed a captured server contract through the real query/render path. The
  // deterministic demo keeps this layout check independent of provider calls.
  await page.evaluate(async () => {
    const modulePath = "/src/lib/state.tsx";
    const { queryClient } = await import(modulePath);
    queryClient.setQueryData(
      ["timeline", "conversation-demo-report"],
      (old: any) => ({
        ...old,
        tasks: old.tasks.map((t: any) =>
          t.id === "demo-report"
            ? {
                ...t,
                progress: {
                  phase: "WAITING_APPROVAL",
                  message: "等待你批准或拒绝动作，继续任务不能代替审批",
                  wait_reason: "APPROVAL_REQUIRED",
                  last_progress_at: "2026-10-07T04:00:00Z",
                  allowed_operations: ["pause", "cancel", "approve", "deny"],
                  action_id: "demo-action",
                },
              }
            : t,
        ),
      }),
    );
  });
  const session = page.getByRole("region", { name: "任务与对话" });
  await expect(
    session.getByRole("region", { name: "任务执行进度" }),
  ).toContainText("等待审批");
  await expect(session.getByText(/最后业务进展/)).toBeVisible();
  await expect(
    session.getByRole("button", { name: "恢复", exact: true }),
  ).toHaveCount(0);
  await page.screenshot({
    path: "test-results/phase2-progress.png",
    fullPage: true,
  });
  await session
    .getByRole("button", { name: "批准执行" })
    .scrollIntoViewIfNeeded();
  await expect(session.getByRole("button", { name: "批准执行" })).toBeVisible();
  await page.screenshot({
    path: "test-results/phase2-progress-minimum.png",
    fullPage: true,
  });
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth),
  ).toBeLessThanOrEqual(900);
});
