import type { WorkspaceMode } from "../api/types";
import { Composer } from "../components/Composer";
import { ConversationView } from "../components/ConversationView";
import { Sidebar } from "../components/Sidebar";
import { useConversation } from "./useConversation";
import { useServiceStatus } from "./useServiceStatus";

function currentMode(pathname: string): WorkspaceMode {
  return pathname === "/agent" ? "agent" : "rag";
}

export function App() {
  const mode = currentMode(window.location.pathname);
  const serviceState = useServiceStatus();
  const { state, send, stop, reset, resetting, notice } = useConversation(mode);

  return (
    <div className="app-shell">
      <a className="skip-link" href="#mainContent">跳到主要内容</a>
      <Sidebar
        mode={mode}
        serviceState={serviceState}
        resetting={resetting}
        onReset={() => void reset()}
      />
      <main className="workspace" id="mainContent">
        <header className="workspace-header">
          <div>
            <p className="eyebrow">{mode === "rag" ? "知识问答" : "Agent"}</p>
            <h2>{mode === "rag" ? "知识问答工作台" : "Agent 工作台"}</h2>
          </div>
          <span className="mode-context">本地单用户模式</span>
        </header>
        <section className="conversation-scroll" aria-label="对话内容">
          {notice && <div className="page-error" role="alert">{notice}</div>}
          <ConversationView mode={mode} state={state} />
        </section>
        <Composer
          mode={mode}
          status={state.status}
          resetting={resetting}
          onSend={send}
          onStop={() => stop(true)}
        />
      </main>
    </div>
  );
}
