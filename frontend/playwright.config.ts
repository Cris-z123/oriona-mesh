import { defineConfig, devices } from "@playwright/test";

const appUrl = "http://127.0.0.1:3100";
const apiBaseUrl = "/v1";

/**
 * Browser tests run the Next app only. API calls are intercepted in
 * `tests/e2e/fixtures/api.ts`, so an E2E run never needs a backend process.
 */
export default defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 2 : 0,
  reporter: process.env.CI ? "github" : "list",
  use: {
    baseURL: appUrl,
    trace: "on-first-retry",
  },
  webServer: {
    // 直接使用根目录已锁定的 Next CLI，避免本机全局 pnpm 版本影响 E2E 环境。
    command: "node ../node_modules/next/dist/bin/next dev --hostname 127.0.0.1 --port 3100",
    url: appUrl,
    reuseExistingServer: !process.env.CI,
    env: {
      ...process.env,
      NEXT_PUBLIC_API_BASE_URL: apiBaseUrl,
    },
  },
  projects: [
    {
      name: "chromium",
      // 本地复用已安装的 Chrome；CI 不提供该 channel 时照常使用 Playwright Chromium。
      use: { ...devices["Desktop Chrome"], channel: process.env.CI ? undefined : "chrome" },
    },
  ],
});
