import { expect, test, type ConsoleMessage } from "@playwright/test";

test("real backend rejects a skill disabled after the browser loaded its list", async ({ page }) => {
  const origin = "http://127.0.0.1:8002";
  await page.goto(`${origin}/agent`);
  await page.getByLabel("账号", { exact: true }).fill("browser-check");
  await page.getByLabel("密码", { exact: true }).fill("verified-browser-password");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Agent 工作台" })).toBeAttached();

  const account = await page.request.get(`${origin}/auth/me`);
  expect(account.ok()).toBeTruthy();
  const headers = { Origin: origin, "X-CSRF-Token": (await account.json()).csrf_token };
  const uploaded = await page.request.post(`${origin}/skills`, {
    headers,
    multipart: { file: {
      name: "browser-skill.md", mimeType: "text/markdown",
      buffer: Buffer.from("---\nschema_version: 1\nname: browser-skill\ndescription: Browser verification\n---\nRespond concisely.\n"),
    } },
  });
  expect(uploaded.status()).toBe(201);
  const skill = await uploaded.json();
  const enabled = await page.request.patch(`${origin}/skills/${skill.id}`, {
    headers, data: { enabled: true, expected_revision: skill.revision },
  });
  expect(enabled.ok()).toBeTruthy();
  const input = page.getByRole("textbox", { name: "向 Agent 发送任务" });
  await input.fill("/");
  await page.getByRole("option", { name: /browser-skill/ }).click();
  await input.fill("/browser-skill 整理部署要点");
  const disabled = await page.request.patch(`${origin}/skills/${skill.id}`, {
    headers, data: { enabled: false, expected_revision: (await enabled.json()).revision },
  });
  expect(disabled.ok()).toBeTruthy();
  const pending = page.waitForResponse(response => response.url() === `${origin}/agent/chat/stream`);
  await page.getByRole("button", { name: "发送", exact: true }).click();
  const rejected = await pending;
  expect(rejected.status()).toBe(422);
  expect(rejected.request().postDataJSON()).toMatchObject({ message: "整理部署要点", skill_ids: [skill.id] });
  expect(await rejected.json()).toMatchObject({ detail: { code: "skill_not_selected" } });
  await expect(page.getByText("所选技能不可用，请刷新技能列表后重试。", { exact: true })).toBeVisible();
  const body = rejected.request().postDataJSON();
  const history = await page.request.get(`${origin}/conversations/${body.conversation_id}/turns`);
  expect(history.ok()).toBeTruthy();
  expect((await history.json()).items).toEqual([]);
});

for (const route of ["/", "/agent", "/settings/skills"] as const) {
  test(`executes the production React entry at ${route} under the strict CSP`, async ({ page }) => {
    const policyErrors: string[] = [];
    page.on("console", (message: ConsoleMessage) => {
      if (message.type() === "error" && /content security policy|refused to/i.test(message.text())) {
        policyErrors.push(message.text());
      }
    });

    const response = await page.goto(route);

    expect(response?.status()).toBe(200);
    const policy = (await response?.allHeaders())?.["content-security-policy"] ?? "";
    expect(policy).toContain("script-src 'self'");
    expect(policy).toContain("style-src 'self'");
    expect(policy).not.toContain("'unsafe-inline'");
    expect(policy).not.toContain("'unsafe-eval'");
    await expect(page.getByRole("heading", {
      name: route === "/" ? "知识问答工作台" : route === "/agent" ? "Agent 工作台" : "Skills 工作台",
    })).toBeAttached();
    await expect(page.locator("#root > .app-shell")).toBeVisible();
    expect(policyErrors).toEqual([]);
  });
}
