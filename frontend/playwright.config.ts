import { defineConfig, devices } from "@playwright/test";

const loopbackNoProxy = "127.0.0.1,localhost";
process.env.NO_PROXY = [process.env.NO_PROXY, loopbackNoProxy].filter(Boolean).join(",");
process.env.no_proxy = process.env.NO_PROXY;

export default defineConfig({
  testDir: "./e2e",
  testIgnore: "production.spec.ts",
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI
    ? [["line"], ["html", { open: "never" }]]
    : "line",
  use: {
    baseURL: "http://127.0.0.1:4173",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  webServer: [
    {
      command: "node e2e/fake-backend.mjs",
      url: "http://127.0.0.1:8000/health",
      reuseExistingServer: false,
      timeout: 30_000,
    },
    {
      command: "npm run dev -- --host 127.0.0.1 --port 4173 --strictPort",
      url: "http://127.0.0.1:4173/static/app/",
      reuseExistingServer: false,
      timeout: 30_000,
    },
  ],
});
