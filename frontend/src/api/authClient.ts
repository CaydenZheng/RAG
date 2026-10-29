import { PublicClientError } from "./errors";
import { isRecord } from "./sse";
import { IS_PAGES_DEMO } from "../app/runtime";

export interface AccountUser {
  id: string;
  username: string;
  role: "member" | "admin";
  must_change_password: boolean;
}

let csrfToken = "";
let accountMode = false;
export const AUTH_EXPIRED_EVENT = "workspace-auth-expired";

export function clearAccount(): void {
  csrfToken = "";
}

export function setAccountMode(enabled: boolean): void {
  accountMode = enabled;
  clearAccount();
}

export function workspaceFetch(path: string, init: RequestInit = {}): Promise<Response> {
  if (IS_PAGES_DEMO) throw new PublicClientError("auth_failed", "模拟演示不连接账号服务。");
  if (!accountMode) return fetch(path, init);
  const headers = new Headers(init.headers);
  const requestCsrfToken = csrfToken;
  if (accountMode && csrfToken) headers.set("X-CSRF-Token", csrfToken);
  return fetch(path, { ...init, headers, credentials: "same-origin", cache: "no-store" }).then((response) => {
    if (accountMode && csrfToken === requestCsrfToken && response.status === 401) window.dispatchEvent(new Event(AUTH_EXPIRED_EVENT));
    return response;
  });
}

const AUTH_ERRORS: Readonly<Record<string, string>> = {
  auth_invalid_credentials: "账号或密码不正确。",
  auth_rate_limited: "登录尝试过于频繁，请稍后再试。",
  auth_password_invalid: "请设置不同的 12 至 128 字符密码。",
  auth_csrf_rejected: "安全校验失败，请刷新后重试。",
  auth_transport_required: "登录需要 HTTPS；本机开发需由管理员明确开启回环 HTTP。",
};

async function authRequest(path: string, signal: AbortSignal, body?: Record<string, string>): Promise<Response> {
  const headers: Headers = new Headers({ Accept: "application/json" });
  if (body) headers.set("Content-Type", "application/json");
  if (csrfToken && path !== "/auth/login") headers.set("X-CSRF-Token", csrfToken);
  const response = await fetch(path, { method: body ? "POST" : "GET", credentials: "same-origin",
    headers, body: body ? JSON.stringify(body) : undefined, cache: "no-store", signal });
  if (!response.ok) {
    const payload: unknown = await response.json().catch(() => null);
    const code = isRecord(payload) && isRecord(payload.detail) && typeof payload.detail.code === "string" ? payload.detail.code : "";
    throw new PublicClientError(response.status === 401 ? "auth_required" : "auth_failed", AUTH_ERRORS[code] ?? "账号服务暂不可用，请重试或联系管理员。");
  }
  return response;
}

export async function readAccount(signal: AbortSignal, credentials?: { username: string; password: string }): Promise<AccountUser> {
  const response = await authRequest(credentials ? "/auth/login" : "/auth/me", signal, credentials);
  const payload: unknown = await response.json();
  if (signal.aborted) throw new DOMException("Aborted", "AbortError");
  if (!isRecord(payload) || !isRecord(payload.user) || typeof payload.csrf_token !== "string" ||
    typeof payload.user.id !== "string" || typeof payload.user.username !== "string" ||
    !["member", "admin"].includes(String(payload.user.role)) || typeof payload.user.must_change_password !== "boolean") {
    throw new PublicClientError("protocol", "账号响应无效，请重试。");
  }
  csrfToken = payload.csrf_token;
  return { id: payload.user.id, username: payload.user.username,
    role: payload.user.role === "admin" ? "admin" : "member", must_change_password: payload.user.must_change_password };
}

export async function endAccountSession(signal: AbortSignal, password?: { current_password: string; new_password: string }): Promise<void> {
  await authRequest(password ? "/auth/password" : "/auth/logout", signal, password ?? {});
  if (signal.aborted) throw new DOMException("Aborted", "AbortError");
  clearAccount();
}
