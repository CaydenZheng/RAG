import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowDown } from "lucide-react";

import type { WorkspaceMode } from "../api/types";
import { Composer } from "../components/Composer";
import { ConversationView } from "../components/ConversationView";
import { Sidebar } from "../components/Sidebar";
import { useConversation } from "./useConversation";
import { useServiceStatus } from "./useServiceStatus";

function currentMode(pathname: string): WorkspaceMode {
  return pathname === "/agent" ? "agent" : "rag";
}

const AUTO_FOLLOW_THRESHOLD_PX = 80;

export function App() {
  const [mode, setMode] = useState(() => currentMode(window.location.pathname));
  const serviceState = useServiceStatus();
  const ragConversation = useConversation("rag");
  const agentConversation = useConversation("agent");
  const activeConversation = mode === "rag" ? ragConversation : agentConversation;
  const scrollRegion = useRef<HTMLElement | null>(null);
  const followingLatest = useRef(true);
  const [showLatest, setShowLatest] = useState(false);
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

  const navigate = useCallback((nextMode: WorkspaceMode) => {
    if (nextMode === mode) return;
    activeConversation.stop(true);
    window.history.pushState({}, "", nextMode === "agent" ? "/agent" : "/");
    setMode(nextMode);
  }, [activeConversation, mode]);

  useEffect(() => {
    const handlePopState = () => {
      activeConversation.stop(true);
      setMode(currentMode(window.location.pathname));
    };
    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, [activeConversation]);

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
    if (followingLatest.current) scrollToLatest();
  }, [state, scrollToLatest]);

  useEffect(() => {
    followingLatest.current = true;
    setShowLatest(false);
  }, [mode]);

  function handleConversationScroll(): void {
    const region = scrollRegion.current;
    if (!region) return;
    const atBottom = region.scrollHeight - region.scrollTop - region.clientHeight <= AUTO_FOLLOW_THRESHOLD_PX;
    followingLatest.current = atBottom;
    setShowLatest(!atBottom);
  }

  return (
    <div className="app-shell">
      <a className="skip-link" href="#mainContent">跳到主要内容</a>
      <Sidebar
        mode={mode}
        serviceState={serviceState}
        resetting={resetting}
        onReset={() => void reset()}
        onNavigate={navigate}
      />
      <main className="workspace" id="mainContent">
        <header className="workspace-header">
          <div>
            <p className="eyebrow">{mode === "rag" ? "知识问答" : "Agent"}</p>
            <h2>{mode === "rag" ? "知识问答工作台" : "Agent 工作台"}</h2>
          </div>
          <span className="mode-context">本地单用户模式</span>
        </header>
        <section
          className="conversation-scroll"
          aria-label="对话内容"
          ref={scrollRegion}
          onScroll={handleConversationScroll}
        >
          {notice && <div className="page-error" role="alert">{notice}</div>}
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
          value={draft}
          onValueChange={setDraft}
          onSend={send}
          onStop={() => stop(true)}
        />
      </main>
    </div>
  );
}
