import { defineConfig, devices } from "@playwright/test";
export default defineConfig({
  testDir: "e2e",
  fullyParallel: false,
  workers: 1,
  timeout: 30000,
  expect: { timeout: 10000 },
  use: {
    baseURL: "http://127.0.0.1:1421",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    { name: "android-pixel", use: { ...devices["Pixel 7"] } },
    {
      name: "android-small",
      use: { ...devices["Pixel 7"], viewport: { width: 360, height: 740 } },
    },
    {
      name: "android-landscape",
      use: {
        ...devices["Pixel 7"],
        viewport: { width: 900, height: 412 },
      },
    },
  ],
  webServer: [
    {
      command:
        "uv run --project ../../services/kernel uvicorn e2e.server:app --host 127.0.0.1 --port 8080",
      url: "http://127.0.0.1:8080/healthz",
      reuseExistingServer: false,
      env: {
        PYTHONPATH: "../../services/kernel/src:.",
        NIUCAI_DATABASE_URL: "sqlite:///./e2e.db",
        NIUCAI_API_TOKEN: "ci-only-mobile-token-00000000000000000000",
        NIUCAI_COOKIE_SECURE: "false",
        NIUCAI_COMPUTER_WEB_ID: "ci-computer",
        NIUCAI_COMPUTER_WEB_UPSTREAM: "",
      },
    },
    {
      command: "npm run build && npx vite preview --host 127.0.0.1",
      url: "http://127.0.0.1:1421",
      reuseExistingServer: false,
    },
  ],
  reporter: [["list"], ["html", { open: "never" }]],
});
