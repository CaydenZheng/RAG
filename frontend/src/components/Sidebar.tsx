import { Bot, BookOpen, Database, History, Plus, Settings2 } from "lucide-react";

import type { WorkspaceMode } from "../api/types";
import type { ServiceState } from "../app/useServiceStatus";
import { ServiceStatus } from "./ServiceStatus";

interface SidebarProps {
  mode: WorkspaceMode;
  serviceState: ServiceState;
  resetting: boolean;
  onReset: () => void;
}

export function Sidebar({ mode, serviceState, resetting, onReset }: SidebarProps) {
  return (
    <aside className="sidebar" aria-label="工作台导航">
      <a className="brand" href="/" aria-label="RAGFlow 首页">
        <span className="brand-mark" aria-hidden="true">R</span>
        <span className="brand-copy">
          <strong>RAGFlow</strong>
          <small>本地 AI 工作台</small>
        </span>
      </a>

      <button className="new-chat-button" type="button" onClick={onReset} disabled={resetting}>
        <Plus size={18} aria-hidden="true" />
        {resetting ? "正在新建" : "新建对话"}
      </button>

      <nav className="mode-nav" aria-label="对话模式">
        <a className="mode-link" data-active={mode === "rag"} href="/" aria-current={mode === "rag" ? "page" : undefined}>
          <BookOpen size={19} aria-hidden="true" />
          <span>知识问答</span>
        </a>
        <a className="mode-link" data-active={mode === "agent"} href="/agent" aria-current={mode === "agent" ? "page" : undefined}>
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
      <ServiceStatus state={serviceState} />
      <p className="local-only-note">仅供本地单用户使用；会话 ID 不是登录身份。</p>
    </aside>
  );
}
