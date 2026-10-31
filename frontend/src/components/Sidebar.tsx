import { Bot, BookOpen, Database, History, Plus, Settings2, Wrench, X } from "lucide-react";
import { useEffect, useRef, type MouseEvent, type ReactNode } from "react";

import type { WorkspaceMode } from "../api/types";
import { extraHref, IS_PAGES_DEMO, knowledgeHref, modeHref, skillsHref } from "../app/runtime";
import type { ServiceState } from "../app/useServiceStatus";
import { ServiceStatus } from "./ServiceStatus";

interface SidebarProps {
  toolsActive?: boolean; settingsActive?: boolean; onTools?:()=>void; onSettings?:()=>void;
  mode: WorkspaceMode;
  resetting: boolean;
  compact: boolean;
  open: boolean;
  onReset: () => void;
  onNavigate: (mode: WorkspaceMode) => void;
  onClose: () => void;
  serviceState: ServiceState;
  serviceDetail?: string;
  onRefreshService: () => void;
  history?: ReactNode;
  account?: boolean;
  skillsActive?: boolean;
  onSkills?: () => void;
  knowledgeActive?: boolean;
  onKnowledge?: () => void;
  username?: string;
  onLogout?: () => void;
}

export function Sidebar({
  toolsActive=false,settingsActive=false,onTools,onSettings,
  mode,
  resetting,
  compact,
  open,
  onReset,
  onNavigate,
  onClose,
  serviceState,
  serviceDetail,
  onRefreshService,
  history,
  account = false,
  skillsActive = false,
  onSkills,
  knowledgeActive = false,
  onKnowledge,
  username,
  onLogout,
}: SidebarProps): React.JSX.Element {
  const closeButton = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (compact && open) closeButton.current?.focus();
  }, [compact, open]);

  function navigate(event: MouseEvent<HTMLAnchorElement>, nextMode: WorkspaceMode): void {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    onNavigate(nextMode);
  }

  return (
    <aside
      className="sidebar"
      id="workspaceNavigation"
      aria-label="工作台导航"
      aria-hidden={compact && !open ? true : undefined}
      data-open={open}
      inert={compact && !open ? true : undefined}
    >
      <div className="sidebar-header">
        <a className="brand" href={modeHref("rag")} aria-label="RAGFlow 首页" onClick={(event) => navigate(event, "rag")}>
          <span className="brand-mark" aria-hidden="true">R</span>
          <span className="brand-copy">
            <strong>RAGFlow</strong>
            <small>{IS_PAGES_DEMO ? "公开模拟演示" : "本地 AI 工作台"}</small>
          </span>
        </a>
        <button ref={closeButton} className="sidebar-close" type="button" onClick={onClose} aria-label="关闭导航">
          <X size={18} aria-hidden="true" />
        </button>
      </div>

      <button className="new-chat-button" type="button" onClick={onReset} disabled={resetting}>
        <Plus size={16} aria-hidden="true" />
        {resetting ? "正在新建" : "新建对话"}
      </button>

      <nav className="mode-nav" aria-label="对话模式">
        <a className="mode-link" data-active={!toolsActive && !settingsActive && !skillsActive && !knowledgeActive && mode === "rag"} href={modeHref("rag")} aria-current={!toolsActive && !settingsActive && !skillsActive && !knowledgeActive && mode === "rag" ? "page" : undefined} onClick={(event) => navigate(event, "rag")}>
          <BookOpen size={16} aria-hidden="true" />
          <span>知识问答</span>
        </a>
        <a className="mode-link" data-active={!toolsActive && !settingsActive && !skillsActive && !knowledgeActive && mode === "agent"} href={modeHref("agent")} aria-current={!toolsActive && !settingsActive && !skillsActive && !knowledgeActive && mode === "agent" ? "page" : undefined} onClick={(event) => navigate(event, "agent")}>
          <Bot size={16} aria-hidden="true" />
          <span>Agent</span>
        </a>
        {(account || IS_PAGES_DEMO) && <a className="mode-link" data-active={skillsActive} aria-current={skillsActive ? "page" : undefined} href={skillsHref()} onClick={(event) => {
          if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
          event.preventDefault();
          onSkills?.();
        }}><Settings2 size={16} aria-hidden="true" /><span>Skills 技能</span></a>}
      </nav>

      <section className="sidebar-section" aria-labelledby="recentTitle">
        <div className="sidebar-section-title" id="recentTitle">
          <History size={14} aria-hidden="true" />
          <span>最近对话</span>
        </div>
        {history ?? <p className="coming-soon">即将支持</p>}
      </section>

      <div className="sidebar-spacer" />
      {(account||IS_PAGES_DEMO)&&<>
        <a className="mode-link" href={extraHref("tools")} data-active={toolsActive} aria-current={toolsActive?"page":undefined} onClick={event=>{if(event.button!==0||event.ctrlKey||event.metaKey||event.altKey||event.shiftKey)return;event.preventDefault();onTools?.();}}><Wrench size={15} aria-hidden="true" /><span>工具与 MCP</span></a>
        <a className="mode-link" href={extraHref("settings")} data-active={settingsActive} aria-current={settingsActive?"page":undefined} onClick={event=>{if(event.button!==0||event.ctrlKey||event.metaKey||event.altKey||event.shiftKey)return;event.preventDefault();onSettings?.();}}><Settings2 size={15} aria-hidden="true" /><span>工作台设置</span></a>
      </>}

      {(account || IS_PAGES_DEMO) ? <a className="mode-link" href={knowledgeHref()} aria-current={knowledgeActive ? "page" : undefined} data-active={knowledgeActive} onClick={event => {
        if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
        event.preventDefault(); onKnowledge?.();
      }}><Database size={15} aria-hidden="true" /><span>知识库浏览</span></a> : <button className="sidebar-disabled" type="button" disabled aria-describedby="knowledgeHint">
        <Database size={15} aria-hidden="true" />
        <span>知识库管理</span>
        <span className="soon-badge" id="knowledgeHint">即将支持</span>
      </button>}
      <a className="sidebar-link" href={IS_PAGES_DEMO ? "https://github.com/CaydenZheng/RAG" : "/docs"}>
        <Settings2 size={15} aria-hidden="true" />
        <span>{IS_PAGES_DEMO ? "源码与部署说明" : "API 文档"}</span>
      </a>

      <div className="sidebar-footer">
        {username && (
          <div className="sidebar-account">
            <span>{username}</span>
            <button type="button" onClick={onLogout}>退出</button>
          </div>
        )}
        <ServiceStatus state={serviceState} detail={serviceDetail} onRefresh={onRefreshService} />
      </div>
    </aside>
  );
}
