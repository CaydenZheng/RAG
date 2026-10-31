import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { ArrowDown, Menu } from "lucide-react";

import type { WorkspaceMode } from "../api/types";
import { isConversationActive } from "../api/types";
import type { AccountUser } from "../api/authClient";
import { HistoryDirectory } from "../components/HistoryDirectory";
import { Composer } from "../components/Composer";
import { ConversationView } from "../components/ConversationView";
import { Sidebar } from "../components/Sidebar";
import { SkillsPage } from "../components/SkillsPage";
import { KnowledgePage } from "../components/KnowledgePage";
import { AttachmentPanel } from "../components/AttachmentPanel";
import { ToolsPage } from "../components/ToolsPage";
import { WorkspaceSettings } from "../components/WorkspaceSettings";
import { ConversationExport } from "../components/ConversationExport";
import { DemoHistory } from "../components/DemoHistory";
import { useTheme } from "./useTheme";
import { readModels, type ModelCatalog, type ModelChoice } from "../api/workspaceClient";
import { toPublicMessage } from "../api/errors";
import { useConversation } from "./useConversation";
import { useServiceStatus } from "./useServiceStatus";
import { currentMode, extraHref, extraRoute, IS_PAGES_DEMO, isKnowledgeRoute, isSkillsRoute, knowledgeHref, modeHref, skillsHref } from "./runtime";

const AUTO_FOLLOW_THRESHOLD_PX = 80;
const COMPACT_NAVIGATION_QUERY = "(max-width: 899px)";

export function App({ account, onLogout }: { account?: AccountUser; onLogout?: () => void }): React.JSX.Element {
  const [mode, setMode] = useState(currentMode);
  const [skillsView, setSkillsView] = useState(isSkillsRoute);
  const [knowledgeView, setKnowledgeView] = useState(isKnowledgeRoute);
  const [extraView,setExtraView]=useState(extraRoute);
  const {theme,changeTheme}=useTheme();
  const utilityView = skillsView || knowledgeView || extraView!==null;
  const view = skillsView ? "skills" : knowledgeView ? "knowledge" : extraView??"conversation";
  const [attachmentSelection, setAttachmentSelection] = useState<{sessionId:string; ids:string[]; pending:boolean}>({sessionId:"", ids:[], pending:false});
  const [models, setModels] = useState<ModelCatalog | null>(null);
  const [modelsNotice, setModelsNotice] = useState("");
  const [modelsRevision, setModelsRevision] = useState(0);
  useEffect(() => {
    if (!account && !IS_PAGES_DEMO) return undefined;
    const controller = new AbortController();
    setModelsNotice("");
    void readModels(controller.signal).then(value => { if (!controller.signal.aborted) setModels(value); })
      .catch(error => { if (!controller.signal.aborted) setModelsNotice(toPublicMessage(error)); });
    return () => controller.abort();
  }, [account, modelsRevision]);
  const [compactNavigation, setCompactNavigation] = useState(() => (
    window.matchMedia?.(COMPACT_NAVIGATION_QUERY).matches ?? false
  ));
  const [navigationOpen, setNavigationOpen] = useState(false);
  const previousMode = useRef(mode);
  const previousView = useRef(view);
  const serviceState = useServiceStatus();
  const ragConversation = useConversation("rag", !!account, models?.defaultModel);
  const agentConversation = useConversation("agent", !!account, models?.defaultModel);
  const activeConversation = mode === "rag" ? ragConversation : agentConversation;
  const scrollRegion = useRef<HTMLElement | null>(null);
  const workspace = useRef<HTMLElement | null>(null);
  const navigationButton = useRef<HTMLButtonElement>(null);
  const historySearch=useRef<HTMLInputElement>(null);
  const confirmDefaultModel=useCallback((choice:ModelChoice)=>setModels(previous=>previous?{...previous,defaultModel:choice}:null),[]);
  const focusAfterNavigationClose = useRef<"navigation" | "workspace" | null>(null);
  const followingLatest = useRef(true);
  const [showLatest, setShowLatest] = useState(false);
  const [routeConversation, setRouteConversation] = useState(() => account ? new URLSearchParams(window.location.search).get("conversation") : null);
  const olderAnchor = useRef<{ height: number; top: number } | null>(null);
  const queuedAgentDraft=useRef<string | null>(null);
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
  useEffect(() => { if (account) workspace.current?.focus({ preventScroll: true }); }, [account]);
  useEffect(() => {
    if (!account || !routeConversation || utilityView) return;
    let current=true;
    void (mode === "rag" ? ragOpen : agentOpen)(routeConversation).then(()=>{
      if(current&&mode==="agent"&&queuedAgentDraft.current!==null){agentConversation.setDraft(queuedAgentDraft.current);queuedAgentDraft.current=null;}
    });
    return ()=>{current=false;queuedAgentDraft.current=null;};
  }, [account, agentOpen, mode, ragOpen, routeConversation, utilityView]);

  const selectionSession = `${mode}:${activeConversation.sessionId}:${activeConversation.summary?.epoch ?? 0}`;
  const receiveAttachments = useCallback((ids: string[], pending: boolean): void => {
    setAttachmentSelection({sessionId:selectionSession, ids, pending});
  }, [selectionSession]);
  const selectedAttachments = attachmentSelection.sessionId === selectionSession ? attachmentSelection.ids : [];
  const attachmentsPending = attachmentSelection.sessionId === selectionSession && attachmentSelection.pending;

  const navigate = useCallback((nextMode: WorkspaceMode): void => {
    if (nextMode === mode && !utilityView) return;
    activeConversation.stop(true);
    const nextId = account ? (nextMode === "rag" ? ragConversation : agentConversation).summary?.id : undefined;
    window.history.pushState({}, "", `${modeHref(nextMode)}${nextId ? `?conversation=${encodeURIComponent(nextId)}` : ""}`);
    setRouteConversation(nextId ?? null);
    setMode(nextMode);
    setSkillsView(false);
    setKnowledgeView(false);
    setExtraView(null);
  }, [account, activeConversation, agentConversation, ragConversation, mode, utilityView]);

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
      const nextSkillsView = isSkillsRoute();
      const nextKnowledgeView = isKnowledgeRoute();
      const nextExtraView=extraRoute();
      const nextId = account ? new URLSearchParams(window.location.search).get("conversation") : null;
      if (account && !nextId && !nextSkillsView && !nextKnowledgeView && !nextExtraView) (nextMode === "rag" ? ragConversation : agentConversation).forget();
      setMode(nextMode);
      setSkillsView(nextSkillsView);
      setKnowledgeView(nextKnowledgeView);
      setExtraView(nextExtraView);
      setRouteConversation(nextId);
    };
    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, [account, activeConversation, agentConversation, ragConversation]);

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
    if (previousMode.current !== mode || previousView.current !== view) {
      workspace.current?.focus({ preventScroll: true });
      previousMode.current = mode;
      previousView.current = view;
    }
  }, [mode, view]);

  function openExtra(next:"tools"|"settings"): void {
    if(extraView===next){if(navigationOpen)closeNavigation();return;}
    activeConversation.stop(true);window.history.pushState({},"",extraHref(next));setExtraView(next);setSkillsView(false);setKnowledgeView(false);setRouteConversation(null);
    if(navigationOpen)closeNavigationToWorkspace();else window.requestAnimationFrame(()=>workspace.current?.focus());
  }

  function prepareAgentExample(text:string): void {
    if(account&&agentConversation.summary)queuedAgentDraft.current=text;
    else agentConversation.setDraft(text);
    navigate("agent");window.requestAnimationFrame(()=>document.getElementById("messageInput")?.focus());
  }

  const newConversation=useCallback(():void=>{
    if(resetting)return;
    if(utilityView)navigate(mode);
    setRouteConversation(null);
    void reset();
    if(navigationOpen)closeNavigationToWorkspace();
  },[closeNavigationToWorkspace,mode,navigate,navigationOpen,reset,resetting,utilityView]);

  useEffect(()=>{
    function shortcuts(event:KeyboardEvent): void {
      if(event.isComposing||event.keyCode===229||event.repeat||event.getModifierState("AltGraph")||!event.altKey||!(event.ctrlKey||event.metaKey)||event.shiftKey||document.querySelector('dialog[open], [role="dialog"]'))return;
      const key=event.key.toLowerCase();
      if(key==="h"&&(account||IS_PAGES_DEMO)){event.preventDefault();if(compactNavigation)setNavigationOpen(true);window.requestAnimationFrame(()=>historySearch.current?.focus());}
      if(key==="i"&&!utilityView){event.preventDefault();if(navigationOpen)closeNavigationToWorkspace();window.requestAnimationFrame(()=>document.getElementById("messageInput")?.focus());}
      if(key==="n"&&!resetting){event.preventDefault();newConversation();}
    }
    window.addEventListener("keydown",shortcuts);return ()=>window.removeEventListener("keydown",shortcuts);
  },[account,closeNavigationToWorkspace,compactNavigation,navigationOpen,newConversation,resetting,utilityView]);

  function handleConversationScroll(): void {
    if (state.turns.length === 0) return;
    const region = scrollRegion.current;
    if (!region) return;
    const atBottom = region.scrollHeight - region.scrollTop - region.clientHeight <= AUTO_FOLLOW_THRESHOLD_PX;
    followingLatest.current = atBottom;
    setShowLatest(!atBottom);
  }

  function loadOlder(): void {
    if (scrollRegion.current) {
      olderAnchor.current = { height: scrollRegion.current.scrollHeight, top: scrollRegion.current.scrollTop };
    }
    followingLatest.current = false;
    void activeConversation.loadOlder();
  }

  return (
    <div className="app-shell">
      <a
        className="skip-link"
        href="#mainContent"
        tabIndex={compactNavigation && navigationOpen ? -1 : undefined}
      >跳到主要内容</a>
      <Sidebar
        toolsActive={extraView==="tools"} settingsActive={extraView==="settings"} onTools={()=>openExtra("tools")} onSettings={()=>openExtra("settings")}
        skillsActive={skillsView}
        knowledgeActive={knowledgeView}
        onKnowledge={() => {
          if (knowledgeView) { if (navigationOpen) closeNavigation(); return; }
          activeConversation.stop(true);
          window.history.pushState({}, "", knowledgeHref());
          setKnowledgeView(true); setSkillsView(false); setExtraView(null); setRouteConversation(null);
          if (navigationOpen) closeNavigationToWorkspace();
          else window.requestAnimationFrame(() => workspace.current?.focus());
        }}
        onSkills={() => {
          if (skillsView) { if (navigationOpen) closeNavigation(); return; }
          activeConversation.stop(true);
          window.history.pushState({}, "", skillsHref());
          setSkillsView(true);
          setKnowledgeView(false);
          setExtraView(null);
          setRouteConversation(null);
          if (navigationOpen) closeNavigationToWorkspace();
          else window.requestAnimationFrame(() => workspace.current?.focus());
        }}
        account={!!account}
        username={account?.username}
        onLogout={onLogout}
        serviceState={serviceState.state}
        serviceDetail={serviceState.detail}
        onRefreshService={serviceState.refresh}
        history={account ? <HistoryDirectory
          searchRef={historySearch}
          mode={mode}
          activeId={activeConversation.sessionId}
          revision={activeConversation.directoryRevision}
          title={activeConversation.summary?.title}
          busy={resetting || isConversationActive(state.status)}
          canMutate
          onRename={(item, nextTitle) => activeConversation.mutateConversation(item, "rename", nextTitle)}
          onClear={(item) => void activeConversation.mutateConversation(item, "clear")}
          onDelete={(item) => void activeConversation.mutateConversation(item, "delete")}
          onOpen={(id) => {
            setSkillsView(false);
            setKnowledgeView(false);
            setExtraView(null);
            activeConversation.stop(true);
            window.history.pushState({}, "", `${modeHref(mode)}?conversation=${encodeURIComponent(id)}`);
            if (id === routeConversation) void activeConversation.open(id);
            else setRouteConversation(id);
            if (navigationOpen) closeNavigationToWorkspace();
            else workspace.current?.focus();
          }}
        /> : IS_PAGES_DEMO?<DemoHistory searchRef={historySearch} onExample={text=>{navigate("agent");agentConversation.setDraft(text);if(navigationOpen)closeNavigationToWorkspace();}} />:undefined}
        mode={mode}
        resetting={resetting}
        compact={compactNavigation}
        open={!compactNavigation || navigationOpen}
        onReset={newConversation}
        onNavigate={(nextMode) => {
          if (nextMode === mode && !utilityView) {
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
        <h1 className="sr-only">
          {extraView==="tools"?"工具目录工作台":extraView==="settings"?"偏好设置工作台":knowledgeView ? "知识库浏览" : skillsView ? "Skills 工作台" : mode === "rag" ? "知识问答工作台" : "Agent 工作台"}
        </h1>
        <button
          ref={navigationButton}
          className="mobile-nav-button"
          type="button"
          aria-label="打开导航"
          aria-controls="workspaceNavigation"
          aria-expanded={navigationOpen}
          onClick={() => setNavigationOpen(true)}
        >
          <Menu size={16} aria-hidden="true" />
        </button>
        {extraView==="tools"?<ToolsPage available={!!account||IS_PAGES_DEMO} onExample={prepareAgentExample} />:extraView==="settings"?(account||IS_PAGES_DEMO?<WorkspaceSettings models={models} onSaved={confirmDefaultModel} onReloadModels={()=>setModelsRevision(value=>value+1)} theme={theme} onTheme={changeTheme} />:<p>登录后可设置账号默认模型。</p>):knowledgeView ? <KnowledgePage available={!!account || IS_PAGES_DEMO} /> : skillsView ? (
          models || (!account && !IS_PAGES_DEMO) ? (
            <SkillsPage available={!!account || IS_PAGES_DEMO} models={models?.items} defaultModel={models?.defaultModel} />
          ) : (
            <section className="skills-page">
              <p role="status">{modelsNotice || "正在加载模型目录…"}</p>
              <button className="btn" type="button" onClick={() => setModelsRevision(value => value + 1)}>重新读取模型目录</button>
            </section>
          )
        ) : (
          <>
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
              {modelsNotice && <div className="page-error" role="alert">{modelsNotice}<button type="button" onClick={() => setModelsRevision(value => value + 1)}>重新读取模型目录</button></div>}
              {account && activeConversation.nextBefore && (
                <div className="load-older">
                  <button type="button" disabled={resetting || isConversationActive(state.status)} onClick={loadOlder}>
                    加载更早问答
                  </button>
                </div>
              )}
              <ConversationView
                mode={mode}
                state={state}
                onInteractionDecision={respondToInteraction}
              />
              {(account||IS_PAGES_DEMO)&&<ConversationExport key={selectionSession} id={activeConversation.sessionId} disabled={resetting||isConversationActive(state.status)||activeConversation.unconfirmed||(!activeConversation.summary&&!IS_PAGES_DEMO)} turns={state.turns} />}
              {showLatest && (
                <button className="back-to-latest" type="button" onClick={scrollToLatest}>
                  <ArrowDown size={14} aria-hidden="true" />回到最新
                </button>
              )}
            </section>
            <Composer
              attachmentsPending={attachmentsPending}
              mode={mode}
              status={state.status}
              resetting={resetting || activeConversation.modelUnconfirmed}
              modelUnavailable={(!!account || IS_PAGES_DEMO) && !models?.items.some(item => item.id === activeConversation.model && item.available)}
              model={activeConversation.model}
              models={account || IS_PAGES_DEMO ? models?.items ?? [] : undefined}
              onModelChange={value => void activeConversation.chooseModel(value)}
              unconfirmed={activeConversation.unconfirmed}
              onReconcile={() => void activeConversation.reconcile()}
              skillsEnabled={!!account || IS_PAGES_DEMO}
              value={draft}
              onValueChange={setDraft}
              onSend={(message, skillIds) => attachmentsPending ? Promise.resolve() : send(message, skillIds, selectedAttachments)}
              onStop={() => stop(true)}
              attachments={(account || IS_PAGES_DEMO) && <AttachmentPanel key={selectionSession}
                sessionId={activeConversation.sessionId} exists={!!activeConversation.summary || IS_PAGES_DEMO}
                disabled={resetting || isConversationActive(state.status) || activeConversation.unconfirmed || activeConversation.modelUnconfirmed}
                ensure={activeConversation.ensure} onSelection={receiveAttachments} onDeleted={activeConversation.markAttachmentDeleted} />}
            />
          </>
        )}
      </main>
    </div>
  );
}
