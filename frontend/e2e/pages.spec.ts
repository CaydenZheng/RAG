import { expect, test } from "@playwright/test";

test.beforeEach(async ({ page }) => {
  await page.goto("");
});

test("tools, preferences, theme, title filtering and export are explicitly simulated",async({page})=>{
  const connections:string[]=[];page.on("request",request=>{if(["fetch","xhr","eventsource","websocket"].includes(request.resourceType()))connections.push(new URL(request.url()).pathname);});
  await page.getByRole("link",{name:"工具与 MCP",exact:true}).click();await expect(page.getByRole("heading",{name:"工具与 MCP",exact:true})).toBeVisible();await expect(page.getByText("预设模拟目录，不连接 Provider 或执行工具。",{exact:true})).toBeVisible();
  await page.getByRole("button",{name:"将计算示例填入 Agent",exact:true}).click();await expect(page.getByRole("textbox",{name:"向 Agent 发送任务"})).toHaveValue(/12 \+ 21/);expect(connections).toEqual([]);
  await page.getByRole("link",{name:"工作台设置",exact:true}).click();await page.getByRole("combobox",{name:"主题",exact:true}).selectOption("dark");await expect(page.locator("html")).toHaveCSS("color-scheme","dark");await page.reload();await expect(page.getByRole("combobox",{name:"主题",exact:true})).toHaveValue("dark");
  await page.getByRole("combobox",{name:"新对话默认模型",exact:true}).selectOption("pro");await page.getByRole("button",{name:"保存默认模型",exact:true}).click();await expect(page.getByText(/默认模型已保存/)).toBeVisible();
  page.once("dialog",dialog=>dialog.accept());await page.getByRole("button",{name:"新建对话",exact:true}).click();await expect(page.getByRole("combobox",{name:"模型",exact:true})).toHaveValue("pro");
  await page.getByRole("textbox",{name:"搜索历史标题"}).fill("表单");await page.getByRole("button",{name:"表单示例（模拟）",exact:true}).click();await page.getByRole("button",{name:"发送",exact:true}).click();await page.getByRole("button",{name:"待处理 1 项，前往处理",exact:true}).click();await expect(page.getByRole("heading",{name:"需要你的输入",exact:true})).toBeFocused();
  await page.getByLabel("示例主题",{exact:false}).fill("合成主题");await page.getByRole("button",{name:"提交并继续",exact:true}).click();await expect(page.locator(".answer-footer")).toBeVisible();
  const pending=page.waitForEvent("download");await page.getByRole("button",{name:"导出演示对话",exact:true}).click();expect((await pending).suggestedFilename()).toBe("simulated-conversation.md");
  await page.keyboard.press("Control+Alt+i");await expect(page.getByRole("textbox",{name:"向 Agent 发送任务"})).toBeFocused();
  expect(connections).toEqual([]);
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
  await expect(page.getByText("静态模拟演示 · 非真实 AI 服务", { exact: true })).toBeInViewport({ ratio: 1 });
  await expect(page.getByText("模拟模式 · 无后端")).toBeVisible();
  const policy = await page.locator('meta[http-equiv="Content-Security-Policy"]').getAttribute("content");
  expect(policy).toContain("connect-src 'none'");
  expect(policy).not.toMatch(/unsafe-inline|unsafe-eval/);
  await page.getByRole("textbox", {name:/向知识库提问|向 Agent 发送任务/}).fill("总结知识库");
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
  await page.getByRole("textbox", {name:/向知识库提问|向 Agent 发送任务/}).fill("开始输出");
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await page.getByRole("link", { name: "Agent", exact: true }).click();
  await expect(page).toHaveURL(/\/RAG\/\?mode=agent$/);
  await expect(page.getByRole("main")).toBeFocused();
  await page.reload();
  await expect(page.getByRole("heading", { name: "Agent 工作台" })).toBeAttached();
  await page.goBack();
  await expect(page.getByRole("heading", { name: "知识问答工作台" })).toBeAttached();
  await page.goForward();
  await expect(page.getByRole("heading", { name: "Agent 工作台" })).toBeAttached();
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
  await page.getByRole("textbox", {name:/向知识库提问|向 Agent 发送任务/}).fill("停止演示");
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await page.getByRole("button", { name: "停止", exact: true }).click();
  await expect(page.getByText("已停止，本轮未确认为成功回答。")).toBeVisible();
  await page.getByRole("textbox", {name:/向知识库提问|向 Agent 发送任务/}).fill("错误");
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("模拟错误终态");
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "新建对话", exact: true }).click();
  await expect(page.locator(".conversation-turn")).toHaveCount(0);
  await page.getByRole("link", { name: "Agent", exact: true }).click();
  await page.getByRole("textbox", {name:/向知识库提问|向 Agent 发送任务/}).fill("超时");
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("模拟 25 秒超时", { timeout: 30_000 });
  await expect(page.getByRole("button", { name: "复制回答" })).toHaveCount(0);
});

test("narrow drawer restores focus and never overflows horizontally", async ({ page }) => {
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

test("reset restores auto-follow after scrolling a long conversation", async ({ page }) => {
  await page.setViewportSize({ width: 1024, height: 460 });
  await page.getByRole("textbox", {name:/向知识库提问|向 Agent 发送任务/}).fill("预设回答");
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await expect(page.getByText("引用来源 · 2")).toBeVisible();
  const region = page.getByRole("region", { name: "对话内容" });
  await region.evaluate((element) => { element.scrollTop = 0; });
  await expect(page.getByRole("button", { name: "回到最新" })).toBeVisible();
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "新建对话", exact: true }).click();
  await expect(page.getByText("静态模拟演示 · 非真实 AI 服务", { exact: true })).toBeInViewport({ ratio: 1 });
  await expect(page.getByRole("button", { name: "回到最新" })).toHaveCount(0);
  await page.getByRole("textbox", {name:/向知识库提问|向 Agent 发送任务/}).fill("新会话回答");
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await expect(page.getByText("引用来源 · 2")).toBeVisible();
  await expect.poll(() => region.evaluate((element) => element.scrollHeight - element.scrollTop - element.clientHeight)).toBeLessThan(80);
  await expect(page.getByRole("button", { name: "回到最新" })).toHaveCount(0);
});

test("models and Skills are explicitly simulated and never contact a backend",async({page})=>{
  const connections:string[]=[];
  page.on("request",request=>{if(["fetch","xhr","websocket","eventsource"].includes(request.resourceType()))connections.push(request.url());});
  await page.getByRole("combobox",{name:"模型",exact:true}).selectOption("pro");
  await page.getByRole("link",{name:"Skills 技能",exact:true}).click();
  await expect(page).toHaveURL(/view=skills/);
  await expect(page.getByText("模拟数据，不连接模型，刷新会重置。",{exact:true})).toBeVisible();
  const toggle = page.getByRole("checkbox",{name:"启用 skill-creator"});
  await toggle.check();
  await expect(toggle).toBeChecked();
  await page.locator('input[type="file"]').setInputFiles({name:"demo.md",mimeType:"text/markdown",buffer:Buffer.from("demo")});
  await expect(page.getByText("技能已上传，当前关闭；请明确启用后使用。",{exact:true})).toBeVisible();
  await page.getByRole("link",{name:"Agent",exact:true}).click();
  await page.getByRole("textbox", {name:/向知识库提问|向 Agent 发送任务/}).fill("/");
  const picker=page.getByRole("listbox",{name:"选择技能"});
  await expect(picker).toBeVisible();
  await expect(picker.getByText("concise-writing")).toBeVisible();
  await expect(picker.getByText("skill-creator")).toHaveCount(0);
  await page.keyboard.press("Enter");
  await expect(page.getByRole("textbox", {name:/向知识库提问|向 Agent 发送任务/})).toHaveValue("/concise-writing ");
  await page.getByRole("textbox", {name:/向知识库提问|向 Agent 发送任务/}).fill("/concise-writing\n模拟任务");
  await expect(picker).toHaveCount(0);
  await expect(page.getByRole("button",{name:"发送",exact:true})).toBeEnabled();
  await page.getByRole("button",{name:"发送",exact:true}).click();
  await expect(page.getByText("模拟任务",{exact:false})).toBeVisible();
  await page.getByRole("link",{name:"Skills 技能",exact:true}).click();
  await page.getByRole("button",{name:"更多操作 模拟上传技能",exact:true}).click();
  await page.getByRole("menuitem",{name:"详情",exact:true}).click();
  await expect(page.getByRole("region",{name:"技能详情"})).toBeVisible();
  await page.getByRole("button",{name:"关闭详情",exact:true}).click();
  await page.reload();
  await expect(page.getByRole("checkbox",{name:"启用 skill-creator"})).not.toBeChecked();
  expect(connections).toEqual([]);
});

test("creator modal restores its complete simulated flow, keyboard focus and narrow layout", async ({page}) => {
  const connections: string[] = [];
  page.on("request", request => { if (["fetch", "xhr", "websocket", "eventsource"].includes(request.resourceType())) connections.push(request.url()); });
  await page.getByRole("link", {name:"Skills 技能", exact:true}).click();
  const open = page.getByRole("button", {name:"从资料生成技能", exact:true});
  await expect(open).toBeDisabled();
  await page.getByRole("checkbox", {name:"启用 skill-creator", exact:true}).click();
  await expect(open).toBeEnabled(); await open.click();
  const dialog = page.getByRole("dialog", {name:"从资料生成技能", exact:true});
  await expect(dialog).toBeVisible();
  for (let index = 0; index < 8; index += 1) {
    await page.keyboard.press("Tab");
    expect(await dialog.evaluate(element => element.contains(document.activeElement))).toBe(true);
  }
  for (let index = 0; index < 8; index += 1) {
    await page.keyboard.press("Shift+Tab");
    expect(await dialog.evaluate(element => element.contains(document.activeElement))).toBe(true);
  }
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0); await expect(open).toBeFocused();
  await open.click();
  await dialog.getByLabel("导入原始资料").setInputFiles({name:"notes.txt", mimeType:"text/plain", buffer:Buffer.from("public synthetic notes")});
  await dialog.getByRole("combobox", {name:"模型", exact:true}).selectOption("pro");
  await dialog.getByRole("button", {name:"开始整理", exact:true}).click();
  await expect(dialog.getByRole("status")).toContainText("模拟转换完成");
  await expect(dialog.getByRole("status")).not.toContainText("技能校验通过");
  expect(await dialog.evaluate(element => element.tagName)).toBe("DIALOG");
  await expect(dialog).toHaveAttribute("open");
  await expect(dialog).not.toHaveAttribute("role");
  const creatorUrl = page.url();
  const preview = await dialog.getByLabel("技能转换预览").innerText();
  const close = dialog.getByRole("button", {name:"关闭生成窗口", exact:true});
  await close.focus();
  for (const modifier of ["Control", "Meta"]) {
    for (const key of ["n", "h", "i"]) {
      await page.keyboard.press(`${modifier}+Alt+${key}`);
      await expect(page).toHaveURL(creatorUrl);
      await expect(dialog).toBeVisible();
      await expect(close).toBeFocused();
      await expect(dialog.getByLabel("技能转换预览")).toHaveText(preview);
      await expect(dialog.getByRole("combobox", {name:"模型", exact:true})).toHaveValue("pro");
    }
  }
  await expect(page.locator(".skill-row")).toHaveCount(2);
  await dialog.getByLabel("导入原始资料").setInputFiles({name:"replacement.txt", mimeType:"text/plain", buffer:Buffer.from("replacement synthetic notes")});
  await expect(dialog.getByLabel("技能转换预览")).toHaveCount(0);
  await expect(dialog.getByRole("link", {name:"下载技能文件", exact:true})).toHaveCount(0);
  await expect(dialog.getByRole("button", {name:"上传此技能（新项默认关闭）", exact:true})).toHaveCount(0);
  await expect(dialog.getByRole("status")).toBeEmpty();
  await dialog.getByRole("button", {name:"开始整理", exact:true}).click();
  await expect(dialog.getByRole("status")).toContainText("模拟转换完成");
  const pendingDownload = page.waitForEvent("download");
  await dialog.getByRole("link", {name:"下载技能文件", exact:true}).click();
  const download = await pendingDownload;
  expect(download.suggestedFilename()).toBe("simulated-writing.md");
  await dialog.getByRole("button", {name:"上传此技能（新项默认关闭）", exact:true}).click();
  await expect(dialog.getByRole("status")).toContainText("当前关闭");
  await expect(page.locator(".skill-row")).toHaveCount(3);
  await dialog.getByRole("button", {name:"关闭生成窗口", exact:true}).click();
  await expect(open).toBeFocused();
  await page.setViewportSize({width:375,height:812});
  await page.emulateMedia({colorScheme:"dark", reducedMotion:"reduce"});
  await open.click(); await expect(dialog).toBeVisible();
  expect(await dialog.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
  await expect(dialog.getByRole("button", {name:"开始整理", exact:true})).toBeDisabled();
  await expect(dialog.getByRole("link", {name:"下载技能文件", exact:true})).toHaveCount(0);
  await page.keyboard.press("Escape"); await expect(open).toBeFocused();
  await page.setViewportSize({width:812,height:375});
  await open.click();
  await expect(dialog.getByRole("button", {name:"关闭生成窗口", exact:true})).toBeInViewport();
  expect(await dialog.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
  await page.keyboard.press("Escape"); await expect(open).toBeFocused();
  expect(connections).toEqual([]);
});

test("attachments and knowledge browsing remain explicit local simulations", async ({page}) => {
  const network: string[] = [];
  page.on("request", request => { if (["fetch","xhr","eventsource","websocket"].includes(request.resourceType())) network.push(request.url()); });
  await page.getByLabel("添加聊天附件").setInputFiles({name:"private-name.txt",mimeType:"text/plain",buffer:Buffer.from("PRIVATE_CONTENT_MUST_NOT_BE_READ")});
  await expect(page.getByRole("checkbox", {name:"本轮使用 模拟短文档.txt"})).toBeChecked();
  await expect(page.getByText("PRIVATE_CONTENT_MUST_NOT_BE_READ")).toHaveCount(0);
  await expect(page.getByText("private-name.txt")).toHaveCount(0);
  await page.getByRole("link", {name:"知识库浏览",exact:true}).click();
  await expect(page).toHaveURL(/view=knowledge/);
  await page.reload();
  await page.getByRole("button", {name:"查看来源摘录：示例使用手册.md",exact:true}).click();
  await expect(page.getByRole("region", {name:"来源摘录"})).toContainText("预设示例");
  await page.setViewportSize({width:375,height:812});
  expect(await page.locator("body").evaluate(element => element.scrollWidth <= window.innerWidth)).toBe(true);
  expect(network).toEqual([]);
});
