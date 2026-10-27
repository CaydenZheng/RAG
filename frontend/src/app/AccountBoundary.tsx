import { useCallback, useEffect, useRef, useState } from "react";

import { AUTH_EXPIRED_EVENT, clearAccount, endAccountSession, readAccount, setAccountMode, type AccountUser } from "../api/authClient";
import { PublicClientError } from "../api/errors";
import { isRecord } from "../api/sse";
import { App } from "./App";
import { IS_PAGES_DEMO } from "./runtime";
import { useTheme } from "./useTheme";
import { ThemeControl } from "../components/ThemeControl";

type AccountState = "checking" | "anonymous" | "login" | "authenticated" | "unavailable";

function AccountThemeControl(): React.JSX.Element {
  const theme = useTheme();
  return <ThemeControl preference={theme.preference} resolved={theme.resolved} onChange={theme.setPreference} />;
}

export function AccountBoundary(): React.JSX.Element {
  const [status, setStatus] = useState<AccountState>(IS_PAGES_DEMO ? "anonymous" : "checking");
  const [user, setUser] = useState<AccountUser | null>(null);
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const operation = useRef<AbortController | null>(null);
  const channel = useRef<BroadcastChannel | null>(null);
  const firstField = useRef<HTMLInputElement>(null);

  const expire = useCallback((): void => {
    operation.current?.abort();
    clearAccount();
    setUser(null);
    setPassword("");
    setNewPassword("");
    setBusy(false);
    setNotice("登录已结束或账号发生变化，请重新登录。");
    setStatus("login");
  }, []);

  useEffect(() => {
    if (IS_PAGES_DEMO) return undefined;
    const controller = new AbortController();
    operation.current = controller;
    void (async () => {
      try {
        const response = await fetch("/workspace/config", { cache: "no-store", signal: controller.signal });
        const config: unknown = await response.json();
        if (!response.ok || !isRecord(config) || typeof config.auth_enabled !== "boolean") throw new Error();
        if (controller.signal.aborted) return;
        setAccountMode(config.auth_enabled);
        if (!config.auth_enabled) { setStatus("anonymous"); return; }
        try {
          const account = await readAccount(controller.signal);
          if (!controller.signal.aborted) { setUser(account); setStatus("authenticated"); }
        } catch (error) {
          if (controller.signal.aborted) return;
          if (error instanceof PublicClientError && error.code === "auth_required") setStatus("login");
          else throw error;
        }
      } catch {
        if (!controller.signal.aborted) setStatus("unavailable");
      }
    })();
    return () => { controller.abort(); clearAccount(); };
  }, [attempt]);

  useEffect(() => {
    if (IS_PAGES_DEMO) return undefined;
    window.addEventListener(AUTH_EXPIRED_EVENT, expire);
    const crossTab = typeof BroadcastChannel === "function" ? new BroadcastChannel("ragflow-account-state") : null;
    channel.current = crossTab;
    if (crossTab) crossTab.onmessage = expire;
    return () => { window.removeEventListener(AUTH_EXPIRED_EVENT, expire); crossTab?.close(); operation.current?.abort(); };
  }, [expire]);

  useEffect(() => { if (status === "login" || user?.must_change_password) firstField.current?.focus(); }, [status, user]);

  // Recheck on focus, not paid health polling; identity changes unmount all runs.
  useEffect(() => {
    if (status !== "authenticated" || !user) return undefined;
    const controller = new AbortController();
    const check = (): void => {
      void readAccount(controller.signal).then((nextUser) => {
        if (!controller.signal.aborted && (nextUser.id !== user.id || nextUser.must_change_password !== user.must_change_password)) expire();
      }).catch((error: unknown) => {
        if (!controller.signal.aborted && error instanceof PublicClientError && error.code === "auth_required") expire();
      });
    };
    window.addEventListener("focus", check);
    return () => { controller.abort(); window.removeEventListener("focus", check); };
  }, [expire, status, user]);

  async function logout(): Promise<void> {
    // Unmount immediately, before the asynchronous credential mutation.
    const controller = new AbortController();
    operation.current?.abort();
    operation.current = controller;
    setStatus("login"); setUser(null); setPassword(""); setNewPassword(""); setBusy(true);
    try {
      await endAccountSession(controller.signal);
      channel.current?.postMessage("changed");
      setNotice("已退出登录。");
    } catch {
      if (!controller.signal.aborted) setNotice("退出尚未确认，请重试退出；不要在共享设备上离开。");
    } finally { if (!controller.signal.aborted) setBusy(false); }
  }

  async function submit(event: React.FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (busy) return;
    const controller = new AbortController();
    operation.current?.abort(); operation.current = controller;
    setBusy(true); setNotice("");
    const changing = user?.must_change_password === true;
    const credentials = { username, password };
    const passwordChange = { current_password: password, new_password: newPassword };
    setPassword(""); setNewPassword("");
    try {
      if (changing) {
        await endAccountSession(controller.signal, passwordChange);
        if (controller.signal.aborted) return;
        setUser(null); setStatus("login"); setNotice("密码已修改，请使用新密码登录。");
      } else {
        const account = await readAccount(controller.signal, credentials);
        if (controller.signal.aborted) return;
        setUser(account); setStatus("authenticated");
      }
      channel.current?.postMessage("changed");
    } catch (error) {
      if (!controller.signal.aborted) { setNotice(error instanceof PublicClientError ? error.message : "网络连接失败，请重试。"); firstField.current?.focus(); }
    } finally { if (!controller.signal.aborted) setBusy(false); }
  }

  if (status === "anonymous") return <App />;
  if (status === "authenticated" && user && !user.must_change_password) return <App key={user.id} account={user} onLogout={() => void logout()} />;
  return (
    <main className="account-shell">
      <section className="account-card" aria-labelledby="accountTitle" aria-busy={busy}>
        <AccountThemeControl />
        <p className="eyebrow">RAGFlow · 邀请制工作台</p>
        <h1 id="accountTitle">{status === "checking" ? "正在检查登录状态" : status === "unavailable" ? "账号服务暂不可用" : user?.must_change_password ? "修改初始密码" : "登录工作台"}</h1>
        {notice && <p role="alert" className="page-error">{notice}</p>}
        {status === "unavailable" ? <button type="button" onClick={() => { setStatus("checking"); setAttempt((value) => value + 1); }}>重新检查</button> : status !== "checking" && (
          <form onSubmit={(event) => void submit(event)}>
            {!user?.must_change_password && <label>账号<input ref={firstField} autoComplete="username" value={username} onChange={(event) => setUsername(event.target.value)} maxLength={64} required disabled={busy} /></label>}
            <label>{user?.must_change_password ? "当前初始密码" : "密码"}<input ref={user?.must_change_password ? firstField : undefined} type={showPassword ? "text" : "password"} autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} maxLength={128} required disabled={busy} /></label>
            {user?.must_change_password && <label>新密码<input type={showPassword ? "text" : "password"} autoComplete="new-password" value={newPassword} onChange={(event) => setNewPassword(event.target.value)} minLength={12} maxLength={128} required disabled={busy} aria-describedby="passwordHint" /></label>}
            <label className="password-toggle"><input type="checkbox" checked={showPassword} onChange={(event) => setShowPassword(event.target.checked)} />显示密码</label>
            {user?.must_change_password && <p id="passwordHint">使用不同的 12 至 128 字符密码；修改后所有设备需要重新登录。</p>}
            <button className="new-chat-button" type="submit" disabled={busy}>{busy ? "正在处理" : user?.must_change_password ? "修改密码" : "登录"}</button>
          </form>
        )}
        {status !== "checking" && <button type="button" disabled={busy} onClick={() => void logout()}>退出当前登录会话</button>}
        <p className="local-only-note">账号由管理员创建，不提供公开注册。知识库共享只读，历史归各账号所有。尚未授权公网部署。</p>
      </section>
    </main>
  );
}
