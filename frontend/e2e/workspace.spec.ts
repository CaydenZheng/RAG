import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

const BACKEND_ORIGIN = "http://127.0.0.1:8000";

async function submit(page: Page, accessibleName: RegExp, message: string): Promise<void> {
  const input = page.getByRole("textbox", { name: accessibleName });
  await input.fill(message);
  await input.press("Enter");
}

async function fakeState(request: APIRequestContext): Promise<{ active_streams: number; actions: string[] }> {
  const response = await request.get(`${BACKEND_ORIGIN}/__test__/state`);
  return response.json();
}

test.beforeEach(async ({ page }) => {
  await page.goto("/");
  await expect(page.getByText("服务可用")).toBeVisible();
});

test("persists theme choice and keeps the empty workspace usable across target widths", async ({ page }) => {
  await expect(page.getByRole("heading", { name: "从知识库中找到可信答案" })).toBeVisible();
  await expect(page.getByRole("button", { name: "添加附件，即将支持" })).toBeDisabled();
  await page.getByRole("combobox", { name: "外观主题" }).selectOption("dark");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.reload();
  await expect(page.getByRole("combobox", { name: "外观主题" })).toHaveValue("dark");
  await page.emulateMedia({ colorScheme: "dark", reducedMotion: "reduce" });
  await page.getByRole("combobox", { name: "外观主题" }).selectOption("system");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  const transitionDuration = await page.locator(".skip-link").evaluate((element) => (
    getComputedStyle(element).transitionDuration
  ));
  expect(Number.parseFloat(transitionDuration)).toBeLessThanOrEqual(0.00001);

  for (const width of [768, 1024, 1440, 1920]) {
    await page.setViewportSize({ width, height: 900 });
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    expect(overflow, `${width}px viewport must not scroll horizontally`).toBeLessThanOrEqual(0);
    await expect(page.getByRole("textbox", { name: /向知识库提问/ })).toBeInViewport();
  }

  await page.setViewportSize({ width: 768, height: 900 });
  await page.getByRole("button", { name: "打开导航" }).click();
  await expect(page.getByRole("button", { name: "关闭导航", exact: true })).toBeFocused();
  await expect(page.getByText("最近对话")).toBeVisible();
  await expect(page.getByRole("button", { name: /知识库管理/ })).toBeVisible();
  await expect(page.getByRole("link", { name: "API 文档" })).toBeVisible();
  await expect(page.getByText("会话 ID 不是登录身份。", { exact: false })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("button", { name: "打开导航" })).toBeFocused();
});

test("moves focus to the workspace when an open compact navigation becomes a desktop sidebar", async ({ page }) => {
  await page.setViewportSize({ width: 768, height: 900 });
  await page.getByRole("button", { name: "打开导航" }).click();
  await expect(page.getByRole("button", { name: "关闭导航", exact: true })).toBeFocused();

  await page.setViewportSize({ width: 1024, height: 900 });

  await expect(page.getByRole("main")).toBeFocused();
  await expect(page.getByRole("button", { name: "关闭导航", exact: true })).toBeHidden();
});

test("returns focus to the menu button after activating the current mode in the compact navigation", async ({ page }) => {
  await page.setViewportSize({ width: 768, height: 900 });
  await page.getByRole("button", { name: "打开导航" }).click();
  const currentMode = page
    .getByRole("complementary", { name: "工作台导航" })
    .getByRole("link", { name: "知识问答" });
  await expect(currentMode).toHaveAttribute("aria-current", "page");

  await currentMode.press("Enter");

  await expect(page.getByRole("button", { name: "打开导航" })).toBeFocused();
});

test("streams RAG chunks before completion, exposes citations, and cancels the upstream", async ({ page, request, context }) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await submit(page, /向知识库提问/, "流式引用");

  await expect(page.getByText("第一段")).toBeVisible();
  expect((await fakeState(request)).active_streams).toBe(1);
  await expect(page.getByText("第一段第二段")).toBeVisible();
  await page.getByText("引用来源 · 1").click();
  await expect(page.getByRole("link", { name: "https://docs.example.test/fixture" })).toBeVisible();
  await expect(page.getByText("精排处理超时，当前结果使用融合排序。")).toBeVisible();
  await page.getByRole("button", { name: "复制回答" }).click();
  await expect(page.getByRole("region", { name: "对话内容" }).getByRole("status")).toContainText("回答已复制");

  await submit(page, /向知识库提问/, "等待停止");
  await expect(page.getByText(/持续生成/)).toBeVisible();
  await page.getByRole("button", { name: "停止" }).click();
  await expect(page.getByText("已停止，本轮未确认为成功回答。")).toBeVisible();
  await expect.poll(async () => (await fakeState(request)).active_streams).toBe(0);
});

test("keeps RAG HTTP and stream failures out of the success state", async ({ page }) => {
  await submit(page, /向知识库提问/, "HTTP 503");
  await expect(page.getByText("服务尚未就绪，请稍后重试。")).toBeVisible();
  await submit(page, /向知识库提问/, "触发错误");
  await expect(page.getByText("回答生成失败，请稍后重试。")).toBeVisible();
  await expect(page.getByText("半截内容")).toBeVisible();
  await expect(page.getByRole("button", { name: "复制回答" })).toHaveCount(0);
});

test("handles tool approval while the Agent stream remains open", async ({ page, request }) => {
  await page.getByRole("link", { name: "Agent" }).click();
  await expect(page.getByRole("main")).toBeFocused();
  await submit(page, /向 Agent 发送任务/, "工具审批");

  await expect(page.getByRole("heading", { name: "需要工具审批" })).toBeVisible();
  const interactions = page.getByLabel("Agent 待处理请求");
  await expect(interactions.getByText("send_message")).toBeVisible();
  await expect(interactions.getByText("local-fake")).toBeVisible();
  await expect(page.getByText(/安全等级/)).toHaveCount(0);
  expect((await fakeState(request)).active_streams).toBe(1);
  await page.getByRole("button", { name: "允许一次" }).click();

  await expect(page.getByText("工具已完成")).toBeVisible();
  await page.getByText("执行过程 · 3 个步骤").click();
  await expect(page.getByText("工具 send_message 已完成")).toBeVisible();
  expect((await fakeState(request)).actions).toContain("approve");
});

test("supports keyboard form Elicitation and explicit URL Elicitation without automatic navigation", async ({ page, context }) => {
  await page.getByRole("link", { name: "Agent" }).click();
  await submit(page, /向 Agent 发送任务/, "表单交互");
  await page.getByRole("button", { name: "提交并继续" }).click();
  await expect(page.getByText("请检查表单中标出的字段。")).toBeFocused();
  await page.getByRole("textbox", { name: /摘要/ }).fill("本地结果");
  await page.getByRole("spinbutton", { name: /数量/ }).fill("2");
  await page.getByRole("button", { name: "提交并继续" }).press("Enter");
  await expect(page.getByText("交互已完成")).toBeVisible();

  await page.reload();
  await submit(page, /向 Agent 发送任务/, "URL 交互");
  await expect(page.getByText("login.example.test")).toBeVisible();
  await expect(page.getByText(/fixture-private-query/)).toHaveCount(0);
  const externalRequests: string[] = [];
  await context.route("https://login.example.test/**", async (route) => {
    externalRequests.push(route.request().url());
    await route.abort();
  });
  expect(externalRequests).toHaveLength(0);
  const popupPromise = page.waitForEvent("popup");
  await page.getByRole("button", { name: "打开安全页面" }).click();
  const popup = await popupPromise;
  await popup.waitForLoadState("domcontentloaded").catch(() => undefined);
  expect(externalRequests).toHaveLength(1);
  await popup.close();
  await page.getByRole("button", { name: "已完成，继续" }).click();
  await expect(page.getByText("交互已完成")).toBeVisible();
});

test("locks an unconfirmed Approval after the 25 second client request boundary", async ({ page }) => {
  await page.clock.install();
  await page.getByRole("link", { name: "Agent" }).click();
  await submit(page, /向 Agent 发送任务/, "审批超时");
  await page.clock.fastForward(350);
  await expect(page.getByRole("button", { name: "允许一次" })).toBeVisible();
  await page.getByRole("button", { name: "允许一次" }).click();
  await expect(page.getByText("正在提交")).toBeVisible();

  await page.clock.fastForward(25_000);

  await expect(page.getByText("结果未知")).toBeVisible();
  await expect(page.getByRole("button", { name: "允许一次" })).toBeDisabled();
});

test("closes Agent stream and polling on route switch", async ({ page, request }) => {
  await page.getByRole("link", { name: "Agent" }).click();
  await submit(page, /向 Agent 发送任务/, "路由取消");
  await expect.poll(async () => (await fakeState(request)).active_streams).toBe(1);

  await page.getByRole("link", { name: "知识问答" }).click();

  await expect.poll(async () => (await fakeState(request)).active_streams).toBe(0);
  await expect(page.getByRole("heading", { name: "知识问答工作台" })).toBeVisible();
});

test("remains keyboard-operable at an effective 200 percent zoom viewport", async ({ page, context }) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await page.setViewportSize({ width: 384, height: 450 });
  await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
  await page.keyboard.press("Tab");
  await expect(page.getByRole("link", { name: "跳到主要内容" })).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("main")).toBeFocused();
  await page.getByRole("textbox", { name: /向知识库提问/ }).fill("等待停止");
  await page.getByRole("textbox", { name: /向知识库提问/ }).press("Enter");
  await page.getByRole("button", { name: "停止" }).press("Enter");
  await expect(page.getByText("已停止，本轮未确认为成功回答。")).toBeVisible();
  await submit(page, /向知识库提问/, "流式引用");
  await expect(page.getByText("第一段第二段")).toBeVisible();
  await page.getByRole("button", { name: "复制回答" }).press("Enter");
  await expect(page.getByRole("region", { name: "对话内容" }).getByRole("status")).toContainText("回答已复制");
  await page.getByRole("button", { name: "打开导航" }).click();
  await page.getByRole("link", { name: "Agent" }).click();
  await submit(page, /向 Agent 发送任务/, "工具审批");
  await page.getByRole("button", { name: "允许一次" }).press("Enter");
  await expect(page.getByText("工具已完成")).toBeVisible();
  await expect(page.getByText("服务可用")).toBeVisible();
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(0);
});

test("renders all documented service states with text", async ({ page, request }) => {
  try {
    const states = [
      ["starting", "服务准备中"],
      ["degraded", "部分能力降级"],
      ["unavailable", "服务暂不可用"],
    ] as const;
    for (const [state, label] of states) {
      await request.get(`${BACKEND_ORIGIN}/__test__/ready/${state}`);
      await page.reload();
      await expect(page.getByText(label)).toBeVisible();
    }
    await request.get(`${BACKEND_ORIGIN}/__test__/ready/ready`);
    await page.getByRole("button", { name: "重新检查服务" }).click();
    await expect(page.getByText("服务可用")).toBeVisible();
  } finally {
    await request.get(`${BACKEND_ORIGIN}/__test__/ready/ready`);
  }
});
