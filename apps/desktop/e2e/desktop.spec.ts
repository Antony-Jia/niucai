import { test, expect } from "@playwright/test";
test("desktop demo renders all seven pages and takes control", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto("/");
  await page.getByRole("button", { name: "体验示例", exact: true }).click();
  await expect(page.getByText("调研 Agent Harness 的最新进展")).toBeVisible();
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
