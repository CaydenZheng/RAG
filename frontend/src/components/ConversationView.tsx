import { useState } from "react";
import { Bot, Check, ChevronRight, CircleAlert, Copy, UserRound } from "lucide-react";

import { isConversationActive, type InteractionDecision, type WorkspaceMode } from "../api/types";
import { safeHttpUrl } from "../api/safeUrls";
import type { ConversationState, ConversationTurn } from "../app/conversationState";
import { MarkdownContent } from "./MarkdownContent";
import { AgentInteractions } from "./AgentInteractions";

const WARNING_MESSAGES: Readonly<Record<string, string>> = Object.freeze({
  bm25_unavailable: "关键词检索暂不可用，当前结果仅使用向量检索。",
  bm25_version_mismatch: "关键词索引与向量索引版本不一致，已自动降级。",
  rerank_timeout: "精排处理超时，当前结果使用融合排序。",
  rerank_unavailable: "精排服务暂不可用，当前结果使用融合排序。",
});
const SOURCE_EXCERPT_LENGTH = 360;

const SUGGESTIONS: Readonly<Record<WorkspaceMode, readonly string[]>> = Object.freeze({
  rag: [
    "总结知识库中的核心观点",
    "当前知识库包含哪些主题？",
    "查找与部署相关的说明",
  ],
  agent: [
    "检索知识库并整理关键结论",
    "计算并解释一个结果",
    "规划一个需要多步工具协作的任务",
  ],
});

function formatLatency(value: number | null): string {
  return value === null ? "—" : `${Math.round(value)} ms`;
}

function EmptyConversation({
  mode,
  onSuggestion,
}: {
  mode: WorkspaceMode;
  onSuggestion: (value: string) => void;
}) {
  return (
    <section className="empty-conversation" aria-labelledby="welcomeTitle">
      <div className="welcome-mark" aria-hidden="true">
        {mode === "rag" ? <ChevronRight size={28} /> : <Bot size={28} />}
      </div>
      <p className="eyebrow">{mode === "rag" ? "知识问答" : "Agent"}</p>
      <h2 id="welcomeTitle">
        {mode === "rag" ? "从知识库中找到可信答案" : "让 Agent 帮你完成多步任务"}
      </h2>
      <p>
        {mode === "rag"
          ? "问题会经过检索与生成，并在完成后提供可核对的引用。"
          : "Agent 会规划步骤、调用后端允许的工具，并展示执行过程。"}
      </p>
      <div className="suggestion-list" aria-label="建议问题">
        {SUGGESTIONS[mode].map((suggestion) => (
          <button key={suggestion} type="button" onClick={() => onSuggestion(suggestion)}>
            {suggestion}
          </button>
        ))}
      </div>
    </section>
  );
}

function ToolProcess({ turn }: { turn: ConversationTurn }) {
  if (!turn.steps.length) return null;
  return (
    <details className="process-panel">
      <summary>执行过程 · {turn.steps.length} 个步骤</summary>
      <ol className="tool-steps">
        {turn.steps.map((step) => (
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

function RagEvidence({ turn }: { turn: ConversationTurn }) {
  if (turn.status !== "completed") return null;
  return (
    <div className="rag-evidence">
      {turn.warnings.map((warning) => (
        <div className="warning-notice" key={warning}>
          <CircleAlert size={17} aria-hidden="true" />
          <span>{WARNING_MESSAGES[warning] ?? "检索过程发生降级。"}</span>
        </div>
      ))}
      <details className="sources-panel">
        <summary>引用来源 · {turn.sources.length}</summary>
        <div className="source-list">
          {turn.sources.length ? (
            turn.sources.map((source, index) => {
              const sourceUrl = safeHttpUrl(source.source);
              return (
                <article className="source-card" key={`${source.ref}-${index}`}>
                  <div className="source-meta">
                    <strong>[{source.ref}]</strong>
                    {sourceUrl ? (
                      <a href={sourceUrl} target="_blank" rel="noopener noreferrer">{source.source}</a>
                    ) : (
                      <span>{source.source}</span>
                    )}
                    <small>{source.score === null ? "相关度 —" : `相关度 ${source.score.toFixed(4)}`}</small>
                  </div>
                  <p>
                    {source.text.slice(0, SOURCE_EXCERPT_LENGTH)}
                    {source.text.length > SOURCE_EXCERPT_LENGTH ? "…" : ""}
                  </p>
                </article>
              );
            })
          ) : (
            <p className="empty-sources">本次回答没有返回引用来源。</p>
          )}
        </div>
      </details>
    </div>
  );
}

export function ConversationView({
  mode,
  state,
  onSuggestion,
  onInteractionDecision,
}: {
  mode: WorkspaceMode;
  state: ConversationState;
  onSuggestion: (value: string) => void;
  onInteractionDecision: (decision: InteractionDecision) => Promise<void>;
}) {
  const [copyFeedback, setCopyFeedback] = useState<{
    mode: WorkspaceMode;
    turn: ConversationTurn;
    message: string;
  } | null>(null);
  const visibleCopyFeedback = copyFeedback?.mode === mode && state.turns.includes(copyFeedback.turn)
    ? copyFeedback
    : null;

  async function copyAnswer(turn: ConversationTurn): Promise<void> {
    try {
      await navigator.clipboard.writeText(turn.answer);
      setCopyFeedback({ mode, turn, message: "回答已复制" });
    } catch {
      setCopyFeedback({ mode, turn, message: "复制失败，请手动选择回答内容" });
    }
  }

  if (state.turns.length === 0) {
    return <EmptyConversation mode={mode} onSuggestion={onSuggestion} />;
  }

  return (
    <div className="conversation" aria-busy={isConversationActive(state.status)}>
      {state.turns.map((turn) => {
        const active = isConversationActive(turn.status);
        return (
          <section className="conversation-turn" key={turn.id}>
            <article className="message message-user">
              <div className="message-avatar" aria-hidden="true"><UserRound size={18} /></div>
              <div>
                <p className="message-author">你</p>
                <p className="message-body">{turn.question}</p>
              </div>
            </article>

            <article className="message message-assistant">
              <div className="message-avatar" aria-hidden="true"><Bot size={18} /></div>
              <div className="assistant-content">
                <p className="message-author">RAGFlow</p>
                {turn.answer ? (
                  <MarkdownContent value={turn.answer} />
                ) : active ? (
                  <p className="stream-status" role="status">
                    {turn.status === "waiting_user" ? "等待你的操作…" : "正在准备回答…"}
                  </p>
                ) : null}
                {active && <span className="stream-cursor" aria-hidden="true" />}
                {turn.status === "cancelled" && <p className="terminal-note">已停止，本轮未保存为成功回答。</p>}
                {turn.status === "failed" && <p className="error-notice" role="alert">{turn.error}</p>}
                {turn.status === "completed" && (
                  <div className="answer-footer">
                    <div className="answer-meta" aria-label="回答元信息">
                      <span>耗时 {formatLatency(turn.latencyMs)}</span>
                      {mode === "rag" ? <span>索引 {turn.indexVersion}</span> : <span>迭代 {turn.iterations ?? "—"}</span>}
                    </div>
                    {turn.answer && (
                      <button
                        className="answer-action"
                        type="button"
                        aria-label="复制回答"
                        onClick={() => void copyAnswer(turn)}
                      >
                        {visibleCopyFeedback?.turn === turn && visibleCopyFeedback.message === "回答已复制"
                          ? <Check size={16} />
                          : <Copy size={16} />}
                      </button>
                    )}
                  </div>
                )}
                {mode === "agent" && <ToolProcess turn={turn} />}
                {mode === "agent" && (
                  <AgentInteractions interactions={turn.interactions} onDecision={onInteractionDecision} />
                )}
                {mode === "rag" && <RagEvidence turn={turn} />}
              </div>
            </article>
          </section>
        );
      })}
      <span className="sr-only" role="status" aria-live="polite">{visibleCopyFeedback?.message ?? ""}</span>
    </div>
  );
}
