import { Bot, ChevronRight, CircleAlert, UserRound } from "lucide-react";

import type { WorkspaceMode } from "../api/types";
import type { ConversationState } from "../app/conversationState";
import { SafeText } from "./SafeText";

const WARNING_MESSAGES: Readonly<Record<string, string>> = Object.freeze({
  bm25_unavailable: "关键词检索暂不可用，当前结果仅使用向量检索。",
  bm25_version_mismatch: "关键词索引与向量索引版本不一致，已自动降级。",
  rerank_timeout: "精排处理超时，当前结果使用融合排序。",
  rerank_unavailable: "精排服务暂不可用，当前结果使用融合排序。",
});

function formatLatency(value: number | null): string {
  return value === null ? "—" : `${Math.round(value)} ms`;
}

function EmptyConversation({ mode }: { mode: WorkspaceMode }) {
  return (
    <section className="empty-conversation" aria-labelledby="welcomeTitle">
      <div className="welcome-mark" aria-hidden="true">
        {mode === "rag" ? <ChevronRight size={28} /> : <Bot size={28} />}
      </div>
      <p className="eyebrow">{mode === "rag" ? "知识问答" : "Agent"}</p>
      <h1 id="welcomeTitle">
        {mode === "rag" ? "从知识库中找到可信答案" : "让 Agent 帮你完成多步任务"}
      </h1>
      <p>
        {mode === "rag"
          ? "问题会经过检索与生成，并在完成后提供可核对的引用。"
          : "Agent 会规划步骤、调用后端允许的工具，并展示执行过程。"}
      </p>
    </section>
  );
}

function ToolProcess({ state }: { state: ConversationState }) {
  if (!state.steps.length) return null;
  return (
    <details className="process-panel" open>
      <summary>执行过程 · {state.steps.length} 个步骤</summary>
      <ol className="tool-steps">
        {state.steps.map((step) => (
          <li key={step.id} className="tool-step" data-success={step.success ?? undefined}>
            <span className="step-index" aria-hidden="true" />
            <div>
              {step.kind === "planning" && <span>第 {step.iteration ?? "—"} 轮规划</span>}
              {step.kind === "tool_call" && (
                <>
                  <span>调用工具：<strong>{step.tool}</strong></span>
                  {step.params && (
                    <details className="parameter-details">
                      <summary>查看参数</summary>
                      <pre>{JSON.stringify(step.params, null, 2)}</pre>
                    </details>
                  )}
                </>
              )}
              {step.kind === "tool_done" && (
                <span>工具 <strong>{step.tool}</strong>{step.success ? " 已完成" : " 执行失败"}</span>
              )}
            </div>
          </li>
        ))}
      </ol>
    </details>
  );
}

function RagEvidence({ state }: { state: ConversationState }) {
  if (state.status !== "completed") return null;
  return (
    <div className="rag-evidence">
      {state.warnings.map((warning) => (
        <div className="warning-notice" key={warning}>
          <CircleAlert size={17} aria-hidden="true" />
          <span>{WARNING_MESSAGES[warning] ?? "检索过程发生降级。"}</span>
        </div>
      ))}
      <details className="sources-panel">
        <summary>引用来源 · {state.sources.length}</summary>
        <div className="source-list">
          {state.sources.length ? (
            state.sources.map((source, index) => (
              <article className="source-card" key={`${source.ref}-${index}`}>
                <div className="source-meta">
                  <strong>[{source.ref}]</strong>
                  <span>{source.source}</span>
                  <small>{source.score === null ? "相关度 —" : `相关度 ${source.score.toFixed(4)}`}</small>
                </div>
                <p>{source.text.slice(0, 360)}{source.text.length > 360 ? "…" : ""}</p>
              </article>
            ))
          ) : (
            <p className="empty-sources">本次回答没有返回引用来源。</p>
          )}
        </div>
      </details>
    </div>
  );
}

export function ConversationView({ mode, state }: { mode: WorkspaceMode; state: ConversationState }) {
  if (state.status === "idle") return <EmptyConversation mode={mode} />;
  const active = state.status === "connecting" || state.status === "streaming";

  return (
    <div className="conversation" aria-busy={active}>
      <article className="message message-user">
        <div className="message-avatar" aria-hidden="true"><UserRound size={18} /></div>
        <div>
          <p className="message-author">你</p>
          <p className="message-body">{state.question}</p>
        </div>
      </article>

      <article className="message message-assistant">
        <div className="message-avatar" aria-hidden="true"><Bot size={18} /></div>
        <div className="assistant-content">
          <p className="message-author">RAGFlow</p>
          {state.answer ? (
            <p className="message-body answer-body"><SafeText value={state.answer} /></p>
          ) : active ? (
            <p className="stream-status" role="status">正在准备回答…</p>
          ) : null}
          {active && <span className="stream-cursor" aria-hidden="true" />}
          {state.status === "cancelled" && <p className="terminal-note">已停止，本轮未保存为成功回答。</p>}
          {state.status === "failed" && <p className="error-notice" role="alert">{state.error}</p>}
          {state.status === "completed" && (
            <div className="answer-meta" aria-label="回答元信息">
              <span>耗时 {formatLatency(state.latencyMs)}</span>
              {mode === "rag" ? <span>索引 {state.indexVersion}</span> : <span>迭代 {state.iterations ?? "—"}</span>}
            </div>
          )}
          {mode === "agent" && <ToolProcess state={state} />}
          {mode === "rag" && <RagEvidence state={state} />}
        </div>
      </article>
    </div>
  );
}
