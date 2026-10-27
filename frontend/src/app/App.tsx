import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { ArrowDown, Menu } from "lucide-react";

import type { WorkspaceMode } from "../api/types";
import { isConversationActive } from "../api/types";
import type { AccountUser } from "../api/authClient";
import { HistoryDirectory } from "../components/HistoryDirectory";
import { Composer } from "../components/Composer";
import { ConversationView } from "../components/ConversationView";
import { ServiceStatus } from "../components/ServiceStatus";
import { Sidebar } from "../components/Sidebar";
import { ThemeControl } from "../components/ThemeControl";
import { useConversation } from "./useConversation";
import { useServiceStatus } from "./useServiceStatus";
import { useTheme } from "./useTheme";
import { currentMode, IS_PAGES_DEMO, modeHref } from "./runtime";

const AUTO_FOLLOW_THRESHOLD_PX = 80;
const COMPACT_NAVIGATION_QUERY = "(max-width: 899px)";

export function App({ account, onLogout }: { account?: AccountUser; onLogout?: () => void }): React.JSX.Element {
  const [mode, setMode] = useState(currentMode);
  const [compactNavigation, setCompactNavigation] = useState(() => (
    window.matchMedia?.(COMPACT_NAVIGATION_QUERY).matches ?? false
  ));
  const [navigationOpen, setNavigationOpen] = useState(false);
  const previousMode = useRef(mode);
  const serviceState = useServiceStatus();
  const theme = useTheme();
  const ragConversation = useConversation("rag", !!account);
  const agentConversation = useConversation("agent", !!account);
  const activeConversation = mode === "rag" ? ragConversation : agentConversation;
  const scrollRegion = useRef<HTMLElement | null>(null);
  const workspace = useRef<HTMLElement | null>(null);
  const navigationButton = useRef<HTMLButtonElement>(null);
  const focusAfterNavigationClose = useRef<"navigation" | "workspace" | null>(null);
  const followingLatest = useRef(true);
  const [showLatest, setShowLatest] = useState(false);
  const [routeConversation, setRouteConversation] = useState(() => account ? new URLSearchParams(window.location.search).get("conversation") : null);
  const [renaming, setRenaming] = useState(false);
  const [titleDraft, setTitleDraft] = useState("");
  const renameButton = useRef<HTMLButtonElement>(null);
  const focusRenameAfterClose = useRef(false);
  const olderAnchor = useRef<{ height: number; top: number } | null>(null);
  const {
    state,
    send,
    stop,
    reset,
    respondToInteraction,
    resetting,
    notice,
    draft,
    setDraft,
  } = activeConversation;

  const ragOpen = ragConversation.open;
  const agentOpen = agentConversation.open;
  useEffect(() => {
    setRenaming(false); setTitleDraft(""); focusRenameAfterClose.current=false;
  }, [mode,activeConversation.sessionId]);
  useEffect(() => { if (account) workspace.current?.focus({preventScroll:true}); }, [account]);
  useLayoutEffect(() => {
    if (!renaming && !resetting && focusRenameAfterClose.current) {
      focusRenameAfterClose.current = false;
      renameButton.current?.focus();
    }
  }, [renaming,resetting]);
  useEffect(() => {
    if (!account || !routeConversation) return;
    void (mode === "rag" ? ragOpen : agentOpen)(routeConversation);
  }, [account,agentOpen,mode,ragOpen,routeConversation]);

  const navigate = useCallback((nextMode: WorkspaceMode): void => {
    if (nextMode === mode) return;
    activeConversation.stop(true);
    const nextId = account ? (nextMode === "rag" ? ragConversation : agentConversation).summary?.id : undefined;
    window.history.pushState({}, "", `${modeHref(nextMode)}${nextId ? `?conversation=${encodeURIComponent(nextId)}` : ""}`);
    setRouteConversation(nextId ?? null);
    setMode(nextMode);
  }, [account,activeConversation,agentConversation,ragConversation,mode]);

  const closeNavigation = useCallback((): void => {
    focusAfterNavigationClose.current = "navigation";
    setNavigationOpen(false);
  }, []);

  const closeNavigationToWorkspace = useCallback((): void => {
    focusAfterNavigationClose.current = "workspace";
    setNavigationOpen(false);
  }, []);

  useEffect(() => {
    const focusDestination = focusAfterNavigationClose.current;
    if (navigationOpen || focusDestination === null) return;
    focusAfterNavigationClose.current = null;
    if (focusDestination === "navigation" && compactNavigation) {
      navigationButton.current?.focus();
      return;
    }
    workspace.current?.focus({ preventScroll: true });
  }, [compactNavigation, navigationOpen]);

  useEffect(() => {
    const media = window.matchMedia?.(COMPACT_NAVIGATION_QUERY);
    if (!media) return undefined;
    const handleChange = (): void => {
      setCompactNavigation(media.matches);
      if (!media.matches && navigationOpen) closeNavigationToWorkspace();
    };
    media.addEventListener("change", handleChange);
    return () => media.removeEventListener("change", handleChange);
  }, [closeNavigationToWorkspace, navigationOpen]);

  useEffect(() => {
    document.body.dataset.navigationOpen = String(compactNavigation && navigationOpen);
    if (!navigationOpen) {
      return () => {
        delete document.body.dataset.navigationOpen;
      };
    }
    const handleKeyDown = (event: KeyboardEvent): void => {
      if (event.key === "Escape") closeNavigation();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("keydown", handleKeyDown);
      delete document.body.dataset.navigationOpen;
    };
  }, [closeNavigation, compactNavigation, navigationOpen]);

  useEffect(() => {
    const handlePopState = (): void => {
      activeConversation.stop(true);
      const nextMode = currentMode();
      const nextId = account ? new URLSearchParams(window.location.search).get("conversation") : null;
      if (account && !nextId) (nextMode === "rag" ? ragConversation : agentConversation).forget();
      setMode(nextMode);
      setRouteConversation(nextId);
    };
    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, [account,activeConversation,agentConversation,ragConversation]);

  useLayoutEffect(() => {
    if (olderAnchor.current && scrollRegion.current) {
      const region = scrollRegion.current;
      region.scrollTop = olderAnchor.current.top + region.scrollHeight - olderAnchor.current.height;
      olderAnchor.current = null;
    }
  }, [state.turns]);
  useEffect(() => { if (!resetting) olderAnchor.current = null; }, [resetting]);

  const scrollToLatest = useCallback(() => {
    const region = scrollRegion.current;
    if (!region) return;
    followingLatest.current = true;
    setShowLatest(false);
    if (typeof region.scrollTo === "function") {
      region.scrollTo({ top: region.scrollHeight, behavior: "auto" });
    }
  }, []);

  useEffect(() => {
    if (state.turns.length === 0) {
      followingLatest.current = true;
      setShowLatest(false);
      if (scrollRegion.current) scrollRegion.current.scrollTop = 0;
      return;
    }
    if (followingLatest.current) scrollToLatest();
  }, [state, scrollToLatest]);

  useEffect(() => {
    followingLatest.current = true;
    setShowLatest(false);
    if (previousMode.current !== mode) {
      workspace.current?.focus({ preventScroll: true });
      previousMode.current = mode;
    }
  }, [mode]);

  function handleConversationScroll(): void {
    if (state.turns.length === 0) return;
    const region = scrollRegion.current;
    if (!region) return;
    const atBottom = region.scrollHeight - region.scrollTop - region.clientHeight <= AUTO_FOLLOW_THRESHOLD_PX;
    followingLatest.current = atBottom;
    setShowLatest(!atBottom);
  }

  return (
    <div className="app-shell">
      <a
        className="skip-link"
        href="#mainContent"
        tabIndex={compactNavigation && navigationOpen ? -1 : undefined}
      >跳到主要内容</a>
      <Sidebar
        account={!!account}
        history={account ? <HistoryDirectory mode={mode} activeId={activeConversation.sessionId} revision={activeConversation.directoryRevision} onOpen={(id) => {
          activeConversation.stop(true);
          window.history.pushState({},"",`${modeHref(mode)}?conversation=${encodeURIComponent(id)}`);
          if (id === routeConversation) void activeConversation.open(id);
          else setRouteConversation(id);
          if (navigationOpen) closeNavigationToWorkspace();
          else workspace.current?.focus();
        }} /> : undefined}
        mode={mode}
        resetting={resetting}
        compact={compactNavigation}
        open={!compactNavigation || navigationOpen}
        onReset={() => { setRouteConversation(null); void reset(); }}
        onNavigate={(nextMode) => {
          if (nextMode === mode) {
            if (navigationOpen) closeNavigation();
            return;
          }
          if (navigationOpen) closeNavigationToWorkspace();
          navigate(nextMode);
        }}
        onClose={closeNavigation}
      />
      {compactNavigation && navigationOpen && (
        <button
          className="sidebar-backdrop"
          type="button"
          tabIndex={-1}
          aria-label="关闭导航菜单背景"
          onClick={closeNavigation}
        />
      )}
      <main
        className="workspace"
        id="mainContent"
        ref={workspace}
        tabIndex={-1}
        aria-hidden={compactNavigation && navigationOpen ? true : undefined}
        inert={compactNavigation && navigationOpen ? true : undefined}
      >
        <header className="workspace-header">
          <button
            ref={navigationButton}
            className="mobile-nav-button"
            type="button"
            aria-label="打开导航"
            aria-controls="workspaceNavigation"
            aria-expanded={navigationOpen}
            onClick={() => setNavigationOpen(true)}
          >
            <Menu size={20} aria-hidden="true" />
          </button>
          <div className="workspace-heading">
            <p className="eyebrow">{mode === "rag" ? "知识问答" : "Agent"}</p>
            <h1>{mode === "rag" ? "知识问答工作台" : "Agent 工作台"}</h1>
          </div>
          <div className="header-actions">
            <span className="mode-context">{IS_PAGES_DEMO ? "公开模拟演示" : account ? `账号：${account.username}` : "本地单用户模式"}</span>
            {account && <button type="button" onClick={onLogout}>退出／切换账号</button>}
            <ServiceStatus state={serviceState.state} onRefresh={serviceState.refresh} />
            <ThemeControl
              preference={theme.preference}
              resolved={theme.resolved}
              onChange={theme.setPreference}
            />
          </div>
        </header>
        <section
          className="conversation-scroll"
          aria-label="对话内容"
          ref={scrollRegion}
          onScroll={handleConversationScroll}
        >
          {IS_PAGES_DEMO && (
            <aside className="demo-notice" aria-label="模拟演示说明">
              <strong>静态模拟演示 · 非真实 AI 服务</strong>
              <p>回答、引用、工具与审批均为预设示例，不连接模型、索引或 MCP，不执行工具。输入仅在当前页面内存中使用，请勿输入敏感信息。刷新会清空对话。</p>
              {mode === "agent" && <p>输入“审批”“表单”“URL”“超时”或“错误”可体验对应流程；其他任务展示模拟工具过程。</p>}
            </aside>
          )}
          {notice && <div className="page-error" role="alert">{notice}</div>}
          {account && <div className="history-actions">
            <p>{activeConversation.summary?.title || "新对话"}</p>
            <button ref={renameButton} type="button" disabled={resetting || !activeConversation.summary || isConversationActive(state.status)} onClick={() => { setTitleDraft(activeConversation.summary?.title || ""); setRenaming(true); }}>重命名</button>
            <button type="button" disabled={resetting || !activeConversation.summary} onClick={() => void activeConversation.mutate("clear").then((saved) => { if (saved) workspace.current?.focus(); })}>清空内容</button>
            <button className="danger-button" type="button" disabled={resetting || !activeConversation.summary} onClick={() => void activeConversation.mutate("delete").then((saved) => { if (saved) workspace.current?.focus(); })}>删除会话</button>
            {renaming && <form onSubmit={(event) => { event.preventDefault(); void activeConversation.mutate("rename",titleDraft).then((saved) => { if (saved) { focusRenameAfterClose.current=true; setRenaming(false); } }); }}>
              <label>会话标题<input autoFocus disabled={resetting} value={titleDraft} onChange={(event) => setTitleDraft(event.target.value)} minLength={1} maxLength={80} required /></label>
              <button type="submit" disabled={resetting}>保存标题</button><button type="button" disabled={resetting} onClick={() => { focusRenameAfterClose.current=true; setRenaming(false); }}>取消</button>
            </form>}
            {activeConversation.unconfirmed && <button type="button" disabled={resetting} onClick={() => void activeConversation.reconcile()}>核对本轮历史</button>}
            {activeConversation.nextBefore && <button type="button" disabled={resetting || isConversationActive(state.status)} onClick={() => {
              if (scrollRegion.current) olderAnchor.current = { height:scrollRegion.current.scrollHeight,top:scrollRegion.current.scrollTop };
              followingLatest.current = false;
              void activeConversation.loadOlder();
            }}>加载更早问答</button>}
          </div>}
          <ConversationView
            mode={mode}
            state={state}
            onSuggestion={setDraft}
            onInteractionDecision={respondToInteraction}
          />
          {showLatest && (
            <button className="back-to-latest" type="button" onClick={scrollToLatest}>
              <ArrowDown size={16} aria-hidden="true" />回到最新
            </button>
          )}
        </section>
        <Composer
          mode={mode}
          status={state.status}
          resetting={resetting}
          unconfirmed={activeConversation.unconfirmed}
          value={draft}
          onValueChange={setDraft}
          onSend={send}
          onStop={() => stop(true)}
        />
      </main>
    </div>
  );
}
