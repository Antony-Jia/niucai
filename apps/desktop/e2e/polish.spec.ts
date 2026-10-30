import { test, expect } from "@playwright/test";

test("all workspace pages fit the viewport and expose connection recovery", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto("/");
  await page.getByRole("button", { name: "体验示例", exact: true }).click();
  const navigation = page
    .getByRole("navigation", { name: "主导航" })
    .getByRole("button");
  for (let i = 0; i < (await navigation.count()); i++) {
    const label = await navigation.nth(i).innerText();
    await navigation.nth(i).click();
    await page.waitForTimeout(150);
    expect(errors, label).toEqual([]);
    await expect(page.locator(".page-content")).toBeVisible();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
  }
  expect(errors).toEqual([]);
});

test("node Bot creation has two clear steps", async ({ page }) => {
  await page.route("**/src/lib/api.ts", async (route) => {
    const response = await route.fetch();
    await route.fulfill({
      response,
      body:
        (await response.text()) +
        `
configureBrowser({
 loadProfile: async () => ({base_url:'http://127.0.0.1:8080',computer_url:'',has_token:true,remember_token:true,name:'界面验收连接',platform:'preview'}),
 request: async (path) => path === '/api/topology' ? {kernel:{remote_enabled:true},nodes:[{id:'n1',name:'本机 Docker Pi',status:'ONLINE',enabled:true,slots:1,busy:0,platform:'linux',engines:{pi:{available:true}},bots:[]}]} : [],
 disconnect: async () => {}, connect: async () => ({}), downloadArtifact: async () => null
});`,
    });
  });
  await page.goto("/");
  await page
    .locator(".sidebar")
    .getByRole("button", { name: /^设置/ })
    .first()
    .click();
  await page.getByRole("tab", { name: "助手" }).click();
  await expect(page.getByText("创建你的第一位 Bot")).toBeVisible();
  await page
    .getByRole("button", { name: "新建 Bot", exact: true })
    .first()
    .click();
  await page.getByRole("button", { name: /本机 Docker Pi/ }).click();
  await page.getByRole("button", { name: "资料研究", exact: true }).click();
  await page.getByLabel("名称（用于 @ 提及）").fill("researcher");
  await expect(page.getByLabel("角色", { exact: true })).toHaveValue(
    "研究与资料整理",
  );
  await expect(
    page.getByRole("button", { name: "保存", exact: true }),
  ).toBeEnabled();
  await page.screenshot({
    path: "test-results/bot-creation.png",
    fullPage: true,
  });
});

for (const width of [1440, 1000, 760]) {
  test(`session navigation keeps the application shell stable at ${width}px`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 1000 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.goto("/");
    await page.getByRole("button", { name: "体验示例", exact: true }).click();
    const nav = page.getByRole("navigation", { name: "主导航" });
    async function shell() {
      await page.mouse.move(width - 5, 5);
      return page.evaluate(() =>
        Object.fromEntries(
          [
            ".sidebar",
            ".brand-mark",
            ".brand small",
            ".topbar",
            ".topbar strong",
            ".sync-status",
            ".sidebar-bottom",
            ".nav-item.active",
            ".nav-item:not(.active)",
          ].map((selector) => {
            const element = document.querySelector(selector)!;
            const style = getComputedStyle(element);
            return [
              selector,
              Object.fromEntries(
                [
                  "width",
                  "height",
                  "padding",
                  "marginBottom",
                  "backgroundColor",
                  "color",
                  "fontSize",
                  "fontWeight",
                  "borderColor",
                  "borderRadius",
                ]
                  .filter(
                    (key) =>
                      !["width", "height"].includes(key) ||
                      [".sidebar", ".topbar"].includes(selector),
                  )
                  .map((key) => [
                    key,
                    style.getPropertyValue(
                      key.replace(/[A-Z]/g, (s) => `-${s.toLowerCase()}`),
                    ),
                  ]),
              ),
            ];
          }),
        ),
      );
    }
    const baseline = await shell();
    for (let i = 0; i < 3; i++) {
      await nav.getByRole("button", { name: /^任务/ }).click();
      await page.locator(".home-task").first().click();
      await expect(
        page.getByRole("region", { name: "任务与对话" }),
      ).toBeVisible();
      expect(await shell()).toEqual(baseline);
      expect(
        await page
          .locator(".workbench")
          .evaluate((el) => getComputedStyle(el).animationName),
      ).toBe("none");
      await nav.getByRole("button", { name: /^文件/ }).click();
      expect(await shell()).toEqual(baseline);
    }
  });
}
