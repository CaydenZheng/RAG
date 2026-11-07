import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AccountBoundary } from "../app/AccountBoundary";
import { AUTH_EXPIRED_EVENT, clearAccount, setAccountMode, workspaceFetch } from "../api/authClient";

const USER = { id: "owner", username: "alice", role: "member", must_change_password: false };
const OLD_CSRF = "a".repeat(64);
const NEW_CSRF = "b".repeat(64);
const json = (body: unknown, status = 200): Response => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const account = (csrf = OLD_CSRF, username = "alice"): Response => json({ user: { ...USER, id: username, username }, csrf_token: csrf });

afterEach(() => { setAccountMode(false); clearAccount(); });

function transport(auth: (path: string, init?: RequestInit) => Promise<Response>) {
  window.history.replaceState({}, "", "/");
  const fetch = vi.fn((path: string, init?: RequestInit): Promise<Response> => {
    if (path.startsWith("/auth/")) return auth(path, init);
    if (path === "/workspace/config") return Promise.resolve(json({ auth_enabled: true }));
    if (path === "/ready") return Promise.resolve(json({ status: "ready" }));
    if (path === "/workspace/models") return Promise.resolve(json({ items: [{ id: "flash", label: "Flash", available: true }], default_model: "flash" }));
    return Promise.resolve(json({ items: [], next_cursor: null }));
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

async function startLogout(): Promise<void> {
  render(<AccountBoundary />);
  fireEvent.click(await screen.findByRole("button", { name: /退出/ }));
}

describe("logout session reconciliation", () => {
  it.each(["missing-cookie", "lost-response"])("returns to login only after /auth/me confirms 401: %s", async scenario => {
    let reads = 0;
    const fetch = transport(async path => {
      if (path === "/auth/me") return ++reads === 1 ? account() : json({}, 401);
      if (scenario === "lost-response") throw new TypeError("response lost");
      return json({ detail: { code: "auth_csrf_rejected" } }, 403);
    });
    await startLogout();
    expect(await screen.findByRole("button", { name: "登录" })).toBeEnabled();
    expect(screen.getByText("已退出登录。")).toBeVisible();
    expect(screen.queryByRole("textbox", { name: "向知识库提问" })).not.toBeInTheDocument();
    expect(reads).toBe(2);
    await workspaceFetch("/ready");
    expect(new Headers(fetch.mock.calls.at(-1)?.[1]?.headers).has("X-CSRF-Token")).toBe(false);
  });

  it("recovers when the first logout and check are offline and the cookie expires before retry", async () => {
    let reads = 0;
    let posts = 0;
    transport(async path => {
      if (path === "/auth/me") {
        reads++;
        if (reads === 1) return account();
        if (reads === 2) throw new TypeError("offline");
        return json({}, 401);
      }
      if (++posts === 1) throw new TypeError("offline");
      return json({ detail: { code: "auth_csrf_rejected" } }, 403);
    });
    await startLogout();
    const retry = await screen.findByRole("button", { name: "重试退出" });
    expect(screen.queryByRole("button", { name: "登录" })).not.toBeInTheDocument();
    fireEvent.click(retry);
    expect(await screen.findByRole("button", { name: "登录" })).toBeEnabled();
    expect(reads).toBe(3);
    expect(posts).toBe(2);
  });

  it.each(["alice", "bob"])("keeps a valid %s session hidden and uses refreshed CSRF only on explicit retry", async username => {
    let reads = 0;
    const tokens: (string | null)[] = [];
    transport(async (path, init) => {
      if (path === "/auth/me") return ++reads === 1 ? account() : account(NEW_CSRF, username);
      tokens.push(new Headers(init?.headers).get("X-CSRF-Token"));
      return tokens.at(-1) === NEW_CSRF ? new Response(null, { status: 204 }) : json({}, 403);
    });
    await startLogout();
    const retry = await screen.findByRole("button", { name: "重试退出" });
    expect(screen.queryByRole("button", { name: "登录" })).not.toBeInTheDocument();
    expect(screen.queryByText("已退出登录。")).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "向知识库提问" })).not.toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(username);
    expect(tokens).toEqual([OLD_CSRF]);
    fireEvent.click(retry);
    expect(await screen.findByRole("button", { name: "登录" })).toBeEnabled();
    expect(tokens).toEqual([OLD_CSRF, NEW_CSRF]);
  });

  it.each([403, 503, 200])("does not accept a failed or malformed /auth/me response (%s) as logout confirmation", async status => {
    let reads = 0;
    transport(async path => path === "/auth/me" ? (++reads === 1 ? account() : json({}, status)) : json({}, 403));
    await startLogout();
    expect(await screen.findByRole("button", { name: "重试退出" })).toBeEnabled();
    expect(reads).toBe(2);
    expect(screen.queryByRole("button", { name: "登录" })).not.toBeInTheDocument();
    expect(screen.queryByText("已退出登录。")).not.toBeInTheDocument();
  });

  it.each([401, 200])("ignores a late reconciliation response (%s) after expiry cancels it and another login succeeds", async status => {
    let reads = 0;
    let finish: ((response: Response) => void) | undefined;
    let signal: AbortSignal | null | undefined;
    const fetch = transport(async (path, init) => {
      if (path === "/auth/login") return account(NEW_CSRF, "bob");
      if (path === "/auth/me") {
        if (++reads === 1) return account();
        signal = init?.signal;
        return new Promise(resolve => { finish = resolve; });
      }
      return json({}, 403);
    });
    await startLogout();
    await waitFor(() => expect(finish).toBeDefined());
    act(() => window.dispatchEvent(new Event(AUTH_EXPIRED_EVENT)));
    expect(signal?.aborted).toBe(true);
    fireEvent.change(screen.getByLabelText("账号"), { target: { value: "bob" } });
    fireEvent.change(screen.getByLabelText("密码"), { target: { value: "synthetic-password" } });
    fireEvent.click(screen.getByRole("button", { name: "登录" }));
    await screen.findByRole("textbox", { name: "向知识库提问" });
    await act(async () => { finish?.(status === 200 ? account(OLD_CSRF) : json({}, 401)); });
    expect(screen.getByRole("textbox", { name: "向知识库提问" })).toBeVisible();
    await workspaceFetch("/ready");
    expect(new Headers(fetch.mock.calls.at(-1)?.[1]?.headers).get("X-CSRF-Token")).toBe(NEW_CSRF);
  });
});
