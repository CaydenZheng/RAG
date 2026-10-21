import { expect, test } from "@playwright/test";

test.beforeEach(async ({ page }) => {
  await page.goto("");
});

test("static assets, CSP and RAG streaming work without any backend", async ({ page }) => {
  const errors: string[] = [];
  const connections: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
  page.on("request", (request) => {
    if (["fetch", "xhr", "websocket", "eventsource"].includes(request.resourceType())) connections.push(request.url());
  });
  page.on("response", (response) => { if (response.status() >= 400) errors.push(`HTTP ${response.status()}`); });
  await page.reload();
  await expect(page.getByLabel("模拟演示说明")).toBeVisible();
  await expect(page.getByText("模拟模式 · 无后端")).toBeVisible();
  const policy = await page.locator('meta[http-equiv="Content-Security-Policy"]').getAttribute("content");
  expect(policy).toContain("connect-src 'none'");
  expect(policy).not.toMatch(/unsafe-inline|unsafe-eval/);
  await page.getByRole("textbox").fill("总结知识库");
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await expect(page.locator(".markdown-content")).toContainText("预设模拟回答");
  await expect(page.getByRole("button", { name: "停止", exact: true })).toBeVisible();
  await expect(page.getByText("引用来源 · 2")).toBeVisible();
  await page.getByText("引用来源 · 2").click();
  await expect(page.getByText("模拟资料 · 工作台介绍")).toBeVisible();
  expect(connections).toEqual([]);
  expect(errors).toEqual([]);
});

test("Agent link supports refresh, history and cancels the previous stream", async ({ page }) => {
  await page.getByRole("textbox").fill("开始输出");
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await page.getByRole("link", { name: "Agent", exact: true }).click();
  await expect(page).toHaveURL(/\/RAG\/\?mode=agent$/);
  await expect(page.getByRole("main")).toBeFocused();
  await page.reload();
  await expect(page.getByRole("heading", { name: "Agent 工作台" })).toBeVisible();
  await page.goBack();
  await expect(page.getByRole("heading", { name: "知识问答工作台" })).toBeVisible();
  await page.goForward();
  await expect(page.getByRole("heading", { name: "Agent 工作台" })).toBeVisible();
});

test("approval, form and URL interactions are explicitly simulated", async ({ page, context }) => {
  await page.getByRole("link", { name: "Agent", exact: true }).click();
  for (const message of ["审批", "表单", "URL"]) {
    await page.getByRole("textbox", { name: "向 Agent 发送任务", exact: true }).fill(message);
    await page.getByRole("button", { name: "发送", exact: true }).click();
    if (message === "审批") {
      await expect(page.getByText("模拟工具（不执行）", { exact: true })).toBeVisible();
      await page.getByRole("button", { name: "允许一次" }).click();
    } else if (message === "表单") {
      await page.getByLabel("示例主题", { exact: false }).fill("演示");
      await page.getByRole("button", { name: "提交并继续" }).click();
    } else {
      expect(context.pages()).toHaveLength(1);
      await expect(page.getByRole("button", { name: "打开安全页面" })).toBeEnabled();
      await page.getByRole("button", { name: "已完成，继续" }).click();
    }
    await expect(page.getByRole("button", { name: "停止", exact: true })).toHaveCount(0);
    await expect(page.locator(".markdown-content").last()).toContainText("模拟任务完成");
  }
  await page.getByText("执行过程 · 3 个步骤").last().click();
  await expect(page.getByText("demo.preview（模拟） 已完成", { exact: false }).last()).toBeVisible();
});

test("stop, error, reset and timeout cannot appear as successful answers", async ({ page }) => {
  test.setTimeout(45_000);
  await page.getByRole("textbox").fill("停止演示");
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await page.getByRole("button", { name: "停止", exact: true }).click();
  await expect(page.getByText("已停止，本轮未保存为成功回答。")).toBeVisible();
  await page.getByRole("textbox").fill("错误");
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("模拟错误终态");
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "新建对话", exact: true }).click();
  await expect(page.locator(".conversation-turn")).toHaveCount(0);
  await page.getByRole("link", { name: "Agent", exact: true }).click();
  await page.getByRole("textbox").fill("超时");
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("模拟 25 秒超时", { timeout: 30_000 });
  await expect(page.getByRole("button", { name: "复制回答" })).toHaveCount(0);
});

test("theme persists and narrow drawer restores focus", async ({ page }) => {
  const theme = page.getByRole("combobox", { name: "主题" });
  await theme.selectOption("dark");
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await theme.selectOption("light");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  await theme.selectOption("system");
  await page.setViewportSize({ width: 375, height: 812 });
  const open = page.getByRole("button", { name: "打开导航", exact: true });
  await open.click();
  await page.keyboard.press("Escape");
  await expect(open).toBeFocused();
  await open.click();
  await page.getByRole("link", { name: "知识问答", exact: true }).click();
  await expect(open).toBeFocused();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});
