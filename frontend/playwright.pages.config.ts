import { defineConfig, devices } from "@playwright/test";

const loopbackNoProxy: string = "127.0.0.1,localhost";
process.env.NO_PROXY = [process.env.NO_PROXY, loopbackNoProxy].filter(Boolean).join(",");
process.env.no_proxy = process.env.NO_PROXY;

export default defineConfig({
  testDir: "./e2e",
  testMatch: "pages.spec.ts",
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: "line",
  use: {
    baseURL: process.env.PAGES_TEST_URL ?? "http://127.0.0.1:4175/RAG/",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "pages-chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: process.env.PAGES_TEST_URL ? undefined : {
    command: "npm run preview:pages",
    url: "http://127.0.0.1:4175/RAG/",
    reuseExistingServer: false,
    timeout: 30_000,
  },
});
