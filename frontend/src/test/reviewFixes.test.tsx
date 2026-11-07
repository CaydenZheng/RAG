import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { AccountBoundary } from "../app/AccountBoundary";
import { readHistory } from "../api/conversationClient";
import { Composer } from "../components/Composer";
import { ConversationView } from "../components/ConversationView";
import { MarkdownContent } from "../components/MarkdownContent";

const json = (body: unknown): Response => new Response(JSON.stringify(body), { headers: { "Content-Type": "application/json" } });

describe("review regressions", () => {
  it.each([{ isComposing: true }, { keyCode: 229 }])("does not send composing Enter %o", (event) => {
    const send = vi.fn();
    render(<Composer mode="rag" status="idle" resetting={false} value="中文问题" onValueChange={vi.fn()} onSend={send} onStop={vi.fn()} />);
    const input = screen.getByRole("textbox");
    fireEvent.keyDown(input, { key: "Enter", ...event });
    expect(send).not.toHaveBeenCalled();
    fireEvent.keyDown(input, { key: "Enter", shiftKey: true });
    expect(send).not.toHaveBeenCalled();
    fireEvent.keyDown(input, { key: "Enter" });
    expect(send).toHaveBeenCalledOnce();
  });

  it("does not select a skill while composing", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(json({ items: [{ id: "s", name: "concise", description: "test", builtin: false, enabled: true, revision: 1, version: "v1" }] })));
    const change = vi.fn();
    render(<Composer mode="agent" status="idle" resetting={false} value="/" skillsEnabled onValueChange={change} onSend={vi.fn()} onStop={vi.fn()} />);
    await screen.findByRole("option");
    fireEvent.keyDown(screen.getByRole("textbox"), { key: "Enter", isComposing: true });
    expect(change).not.toHaveBeenCalled();
    fireEvent.keyDown(screen.getByRole("textbox"), { key: "Enter" });
    expect(change).toHaveBeenCalledWith("/concise ");
  });

  it.each([{ warnings: [] }, { warnings: ["bm25_unavailable", "rerank_timeout"] }])("keeps history notes separate from warnings %o", async ({ warnings }) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(json({ items: [{ seq: 1, question: "q", answer: "a", client_turn_id: "turn", warnings }] })));
    const history = await readHistory("session", new AbortController().signal);
    expect(history.items[0].warnings).toEqual(warnings);
    render(<ConversationView mode="rag" state={{ status: "idle", turns: history.items, nextTurnId: 2 }} onInteractionDecision={vi.fn()} />);
    expect(screen.getByText("历史仅回看已完成问答和回答时的来源，不重放工具过程。")).toBeVisible();
    expect(screen.queryByText("检索过程发生降级。")).not.toBeInTheDocument();
    if (warnings.length) {
      expect(screen.getByText("关键词检索暂不可用，当前结果仅使用向量检索。")).toBeVisible();
      expect(screen.getByText("精排处理超时，当前结果使用融合排序。")).toBeVisible();
    }
  });

  it("renders external Markdown images without automatic resource loading", () => {
    const { container } = render(<MarkdownContent value={'![synthetic](https://example.invalid/collect?data=synthetic)\n\n[安全链接](https://example.test)\n\n`code`'} />);
    expect(container.querySelector("img")).toBeNull();
    expect(screen.getByText(/synthetic/)).toBeVisible();
    expect(screen.getByRole("link", { name: "安全链接" })).toHaveAttribute("href", "https://example.test");
    expect(screen.getByText("code").tagName).toBe("CODE");
  });

  it.each(["network", "server"])("hides private content and allows logout retry after %s failure", async (failure) => {
    window.history.replaceState({}, "", "/");
    let attempts = 0;
    vi.stubGlobal("fetch", vi.fn((path: string) => {
      if (path === "/workspace/config") return Promise.resolve(json({ auth_enabled: true }));
      if (path === "/auth/me") return Promise.resolve(json({ user: { id: "owner", username: "alice", role: "member", must_change_password: false }, csrf_token: "c".repeat(64) }));
      if (path === "/auth/logout") {
        attempts++;
        if (attempts === 1) return failure === "network" ? Promise.reject(new TypeError("offline")) : Promise.resolve(new Response("{}", { status: 503 }));
        return Promise.resolve(new Response(null, { status: 204 }));
      }
      if (path === "/ready") return Promise.resolve(json({ status: "ready" }));
      if (path === "/workspace/models") return Promise.resolve(json({ items: [{ id: "flash", label: "Flash", available: true }], default_model: "flash" }));
      return Promise.resolve(json({ items: [], next_cursor: null }));
    }));
    render(<AccountBoundary />);
    fireEvent.click(await screen.findByRole("button", { name: /退出/ }));
    await screen.findByText(/退出尚未确认/);
    expect(screen.queryByRole("textbox", { name: "向知识库提问" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重试退出" }));
    await waitFor(() => expect(screen.getByText("已退出登录。")).toBeVisible());
    expect(attempts).toBe(2);
    expect(screen.getByRole("button", { name: "登录" })).toBeEnabled();
  });
});
