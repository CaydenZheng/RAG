import { expect, test, type ConsoleMessage } from "@playwright/test";

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
    })).toBeVisible();
    await expect(page.locator("#root > .app-shell")).toBeVisible();
    expect(policyErrors).toEqual([]);
  });
}
