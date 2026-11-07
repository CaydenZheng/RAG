import { expect, test, type Page } from "@playwright/test";

const origin = "http://127.0.0.1:8002";

async function login(page: Page): Promise<void> {
  await page.goto(`${origin}/`);
  await page.getByLabel("账号", { exact: true }).fill("browser-check");
  await page.getByLabel("密码", { exact: true }).fill("verified-browser-password");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page.getByRole("textbox", { name: "向知识库提问" })).toBeVisible();
}

async function expectMissingCookieRecovery(page: Page, button: string | RegExp): Promise<void> {
  const rejected = page.waitForResponse(response => response.url() === `${origin}/auth/logout`);
  const checked = page.waitForResponse(response => response.url() === `${origin}/auth/me`);
  await page.getByRole("button", { name: button }).click();
  const response = await rejected;
  expect(response.status()).toBe(403);
  expect((await response.json()).detail.code).toBe("auth_csrf_rejected");
  const headers = await response.request().allHeaders();
  expect(headers.cookie ?? "").not.toContain("ragflow_login=");
  expect(headers["x-csrf-token"]).toHaveLength(64);
  expect((await checked).status()).toBe(401);
  await expect(page.getByRole("button", { name: "登录", exact: true })).toBeVisible();
  await expect(page.getByRole("textbox", { name: "向知识库提问" })).toHaveCount(0);
  await expect(page.getByText("已退出登录。")).toBeVisible();
}

export function logoutRecoveryScenarios(): void {
  test("logout recovery: removed browser cookie gets real CSRF rejection then confirms expired session", async ({ page, context }) => {
    await login(page);
    await context.clearCookies({ name: "ragflow_login" });
    await expectMissingCookieRecovery(page, /退出/);
  });

  test("logout recovery: network returns after cookie expiry and retry converges", async ({ page, context }) => {
    await login(page);
    let offline = true;
    await page.route(/\/auth\/(logout|me)$/, route => offline ? route.abort("internetdisconnected") : route.continue());
    await page.getByRole("button", { name: /退出/ }).click();
    await expect(page.getByRole("button", { name: "重试退出" })).toBeEnabled();
    await expect(page.getByRole("textbox", { name: "向知识库提问" })).toHaveCount(0);
    await context.clearCookies({ name: "ragflow_login" });
    offline = false;
    await expectMissingCookieRecovery(page, "重试退出");
  });

  test("logout recovery: real server logout with a lost response converges and notifies another tab", async ({ page, context }) => {
    await login(page);
    const other = await context.newPage();
    await other.goto(`${origin}/`);
    await expect(other.getByRole("textbox", { name: "向知识库提问" })).toBeVisible();
    let posts = 0;
    await page.route("**/auth/logout", async route => {
      posts++;
      const result = await route.fetch();
      expect(result.status()).toBe(204);
      await route.abort("failed");
    });
    const checked = page.waitForResponse(response => response.url() === `${origin}/auth/me`);
    await page.getByRole("button", { name: /退出/ }).click();
    expect((await checked).status()).toBe(401);
    await expect(page.getByText("已退出登录。")).toBeVisible();
    await expect(other.getByRole("button", { name: "登录", exact: true })).toBeVisible();
    await expect(other.getByRole("textbox", { name: "向知识库提问" })).toHaveCount(0);
    expect(posts).toBe(1);
  });

  test("logout recovery: replaced credential refreshes CSRF and requires explicit retry for the valid session", async ({ page }) => {
    await login(page);
    // Change the real cookie without updating the page's in-memory CSRF token.
    const replacement = await page.request.post(`${origin}/auth/login`, {
      headers: { Origin: origin }, data: { username: "browser-check", password: "verified-browser-password" },
    });
    expect(replacement.status()).toBe(200);
    const csrf = (await replacement.json()).csrf_token;
    const first = page.waitForResponse(response => response.url() === `${origin}/auth/logout`);
    const checked = page.waitForResponse(response => response.url() === `${origin}/auth/me`);
    await page.getByRole("button", { name: /退出/ }).click();
    expect((await first).status()).toBe(403);
    expect((await checked).status()).toBe(200);
    await expect(page.getByRole("button", { name: "重试退出" })).toBeEnabled();
    await expect(page.getByRole("alert")).toContainText("browser-check");
    await expect(page.getByText("已退出登录。")).toHaveCount(0);
    await expect(page.getByRole("textbox", { name: "向知识库提问" })).toHaveCount(0);
    const retried = page.waitForResponse(response => response.url() === `${origin}/auth/logout`);
    await page.getByRole("button", { name: "重试退出" }).click();
    const response = await retried;
    expect(response.status()).toBe(204);
    expect((await response.request().allHeaders())["x-csrf-token"]).toBe(csrf);
    await expect(page.getByText("已退出登录。")).toBeVisible();
  });
}
