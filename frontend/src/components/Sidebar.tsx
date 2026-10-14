import { Bot, BookOpen, Database, History, Plus, Settings2, X } from "lucide-react";
import { useEffect, useRef, type MouseEvent } from "react";

import type { WorkspaceMode } from "../api/types";

interface SidebarProps {
  mode: WorkspaceMode;
  resetting: boolean;
  compact: boolean;
  open: boolean;
  onReset: () => void;
  onNavigate: (mode: WorkspaceMode) => void;
  onClose: () => void;
}

export function Sidebar({
  mode,
  resetting,
  compact,
  open,
  onReset,
  onNavigate,
  onClose,
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
        <a className="brand" href="/" aria-label="RAGFlow 首页" onClick={(event) => navigate(event, "rag")}>
          <span className="brand-mark" aria-hidden="true">R</span>
          <span className="brand-copy">
            <strong>RAGFlow</strong>
            <small>本地 AI 工作台</small>
          </span>
        </a>
        <button ref={closeButton} className="sidebar-close" type="button" onClick={onClose} aria-label="关闭导航">
          <X size={20} aria-hidden="true" />
        </button>
      </div>

      <button className="new-chat-button" type="button" onClick={onReset} disabled={resetting}>
        <Plus size={18} aria-hidden="true" />
        {resetting ? "正在新建" : "新建对话"}
      </button>

      <nav className="mode-nav" aria-label="对话模式">
        <a className="mode-link" data-active={mode === "rag"} href="/" aria-current={mode === "rag" ? "page" : undefined} onClick={(event) => navigate(event, "rag")}>
          <BookOpen size={19} aria-hidden="true" />
          <span>知识问答</span>
        </a>
        <a className="mode-link" data-active={mode === "agent"} href="/agent" aria-current={mode === "agent" ? "page" : undefined} onClick={(event) => navigate(event, "agent")}>
          <Bot size={19} aria-hidden="true" />
          <span>Agent</span>
        </a>
      </nav>

      <section className="sidebar-section" aria-labelledby="recentTitle">
        <div className="sidebar-section-title" id="recentTitle">
          <History size={16} aria-hidden="true" />
          <span>最近对话</span>
        </div>
        <p className="coming-soon">即将支持</p>
      </section>

      <div className="sidebar-spacer" />

      <button className="sidebar-disabled" type="button" disabled aria-describedby="knowledgeHint">
        <Database size={18} aria-hidden="true" />
        <span>知识库管理</span>
        <span className="soon-badge" id="knowledgeHint">即将支持</span>
      </button>
      <a className="sidebar-link" href="/docs">
        <Settings2 size={18} aria-hidden="true" />
        <span>API 文档</span>
      </a>
      <p className="local-only-note">仅供本地单用户使用；会话 ID 不是登录身份。</p>
    </aside>
  );
}
