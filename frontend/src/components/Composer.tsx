import { ArrowUp, Paperclip, Square } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import type { ConversationStatus, WorkspaceMode } from "../api/types";

interface ComposerProps {
  mode: WorkspaceMode;
  status: ConversationStatus;
  resetting: boolean;
  onSend: (message: string) => Promise<void>;
  onStop: () => void;
}

function isRunning(status: ConversationStatus): boolean {
  return status === "connecting" || status === "streaming";
}

export function Composer({ mode, status, resetting, onSend, onStop }: ComposerProps) {
  const [value, setValue] = useState("");
  const textarea = useRef<HTMLTextAreaElement>(null);
  const running = isRunning(status);
  const blocked = running || resetting;

  useEffect(() => {
    if (!blocked) textarea.current?.focus();
  }, [blocked]);

  const submit = (): void => {
    const message = value.trim();
    if (!message || blocked) return;
    void onSend(message);
  };

  return (
    <div className="composer-wrap">
      <form
        className="composer"
        aria-busy={blocked}
        onSubmit={(event) => {
          event.preventDefault();
          submit();
        }}
      >
        <div className="composer-label-row">
          <label htmlFor="messageInput">
            {mode === "rag" ? "向知识库提问" : "向 Agent 发送任务"}
          </label>
          <span>最多 2000 字</span>
        </div>
        <textarea
          id="messageInput"
          ref={textarea}
          rows={3}
          maxLength={2000}
          value={value}
          readOnly={blocked}
          placeholder={
            mode === "rag"
              ? "向知识库提问，Enter 发送，Shift + Enter 换行"
              : "描述需要 Agent 完成的任务，Enter 发送"
          }
          onChange={(event) => setValue(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              submit();
            }
            if (event.key === "Escape" && running) onStop();
          }}
          autoFocus
        />
        <div className="composer-actions">
          <div className="composer-placeholders">
            <button type="button" className="icon-button" disabled aria-label="添加附件，即将支持">
              <Paperclip size={18} aria-hidden="true" />
            </button>
            <button type="button" className="model-placeholder" disabled>
              默认模型 · 即将支持
            </button>
          </div>
          <span className="character-count" aria-label={`已输入 ${value.length} 个字符`}>
            {value.length}/2000
          </span>
          {running ? (
            <button className="stop-button" type="button" onClick={onStop}>
              <Square size={16} fill="currentColor" aria-hidden="true" />
              停止
            </button>
          ) : (
            <button className="send-button" type="submit" disabled={resetting || !value.trim()}>
              <ArrowUp size={18} aria-hidden="true" />
              <span className="sr-only">{resetting ? "正在新建对话，暂不可发送" : "发送"}</span>
            </button>
          )}
        </div>
      </form>
      <p className="ai-disclaimer">内容由 AI 生成，请核对引用与执行结果。</p>
    </div>
  );
}
