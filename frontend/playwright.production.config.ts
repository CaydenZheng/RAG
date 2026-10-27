import { defineConfig, devices } from "@playwright/test";
import { fileURLToPath } from "node:url";

const loopbackNoProxy = "127.0.0.1,localhost";
process.env.NO_PROXY = [process.env.NO_PROXY, loopbackNoProxy].filter(Boolean).join(",");
process.env.no_proxy = process.env.NO_PROXY;

export default defineConfig({
  testDir: "./e2e",
  testMatch: "production.spec.ts",
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI
    ? [["line"], ["html", { open: "never" }]]
    : "line",
  use: {
    baseURL: "http://127.0.0.1:8001",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "chromium-production",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  webServer: {
    command: "uv run --no-sync --offline --no-env-file uvicorn app:app --host 127.0.0.1 --port 8001 --lifespan off",
    cwd: "..",
    env: {
      AUTH_ENABLED: "false",
      CHROMA_PERSIST_DIR: fileURLToPath(new URL("../.test-tmp/account-history-production/chroma", import.meta.url)),
      CACHE_DB_PATH: fileURLToPath(new URL("../.test-tmp/account-history-production/cache.db", import.meta.url)),
      AUTH_DB_PATH: fileURLToPath(new URL("../.test-tmp/account-history-production/auth.db", import.meta.url)),
      ADMIN_API_KEY: "playwright-production-admin-fixture-key",
      OPENAI_API_KEY: "playwright-production-fixture-key",
    },
    url: "http://127.0.0.1:8001/health",
    reuseExistingServer: false,
    timeout: 60_000,
  },
});
