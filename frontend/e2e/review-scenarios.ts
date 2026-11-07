import { expect, test } from "@playwright/test";

export function reviewScenarios(): void {
  test("review: composing Enter keeps the draft and Markdown images never make requests", async ({ page }) => {
    let queries = 0;
    const external: string[] = [];
    await page.route("https://example.invalid/**", async route => { external.push(route.request().url()); await route.abort(); });
    await page.route("**/workspace/config", route => route.fulfill({ json: { auth_enabled: false } }));
    await page.route("**/ready", route => route.fulfill({ json: { status: "ready" } }));
    await page.route("**/query/stream?**", async route => {
      queries++;
      const answer = "**Synthetic answer**\n\n![synthetic](https://example.invalid/collect?data=synthetic)";
      const events = [
        { event: "chunk", done: false, query_id: "fixture", chunk: answer },
        { event: "done", done: true, query_id: "fixture", session_id: new URL(route.request().url()).searchParams.get("session_id"), answer, sources: [], warnings: [], index_version: "test", latency_ms: 1 },
      ];
      await route.fulfill({ contentType: "text/event-stream", body: events.map(event => `data: ${JSON.stringify(event)}\n\n`).join("") });
    });
    const response = await page.goto("/");
    expect((await response?.allHeaders())?.["content-security-policy"]).toContain("img-src 'self' data:");
    const input = page.getByRole("textbox", { name: "向知识库提问" });
    await input.fill("中文问题");
    await input.dispatchEvent("compositionstart");
    await input.dispatchEvent("keydown", { key: "Enter", code: "Enter", isComposing: true });
    await expect(input).toHaveValue("中文问题");
    expect(queries).toBe(0);
    await input.dispatchEvent("compositionend", { data: "中文问题" });
    await input.press("Enter");
    await expect(page.getByText("Synthetic answer", { exact: true })).toBeVisible();
    await expect(page.getByText("图片：synthetic（未加载）")).toBeVisible();
    await expect(page.locator(".markdown-content img")).toHaveCount(0);
    expect(external).toEqual([]);
    expect(queries).toBe(1);
  });

  test("review: normal history has a replay note and a lost logout response is reconciled", async ({ page }) => {
    const entry = { id: "saved", mode: "rag", title: "saved", revision: 1, epoch: 0, updated_at: "2026-11-04T00:00:00Z", model: "flash" };
    let loggedOut = false;
    let attempts = 0;
    await page.route(/\/(workspace\/(config|models)|auth\/|conversations|ready)/, async route => {
      const path = new URL(route.request().url()).pathname;
      if (path === "/workspace/config") return route.fulfill({ json: { auth_enabled: true } });
      if (path === "/auth/me") return route.fulfill({ status: loggedOut ? 401 : 200, json: { user: { id: "owner", username: "alice", role: "member", must_change_password: false }, csrf_token: "f".repeat(64) } });
      if (path === "/auth/logout") {
        loggedOut = true;
        attempts++;
        return attempts === 1 ? route.abort("failed") : route.fulfill({ status: 204 });
      }
      if (path === "/ready") return route.fulfill({ json: { status: "ready" } });
      if (path === "/workspace/models") return route.fulfill({ json: { items: [{ id: "flash", label: "Flash", available: true }], default_model: "flash" } });
      if (path.endsWith("/turns")) return route.fulfill({ json: { items: [{ seq: 1, question: "Synthetic history", answer: "Synthetic saved answer", client_turn_id: "turn", sources: [], warnings: [] }], next_before: null } });
      return route.fulfill({ json: path === "/conversations" ? { items: [entry], next_cursor: null } : entry });
    });
    await page.goto("/?conversation=saved");
    await expect(page.getByText("Synthetic saved answer", { exact: true })).toBeVisible();
    await expect(page.getByText("历史仅回看已完成问答和回答时的来源，不重放工具过程。")).toBeVisible();
    await expect(page.getByText("检索过程发生降级。")).toHaveCount(0);
    await page.getByRole("button", { name: /退出/ }).click();
    await expect(page.getByText("已退出登录。")).toBeVisible();
    await expect(page.getByText("Synthetic saved answer", { exact: true })).toHaveCount(0);
    expect(attempts).toBe(1);
    await page.reload();
    await expect(page.getByRole("button", { name: "登录", exact: true })).toBeVisible();
  });
}
