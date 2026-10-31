import { useEffect, useRef, useState, type RefObject } from "react";

import { listConversations, type ConversationSummary } from "../api/conversationClient";
import { toPublicMessage } from "../api/errors";
import type { WorkspaceMode } from "../api/types";
import { RowMenu } from "./RowMenu";

interface HistoryDirectoryProps {
  searchRef?: RefObject<HTMLInputElement | null>;
  mode: WorkspaceMode;
  activeId: string;
  revision: number;
  onOpen: (id: string) => void;
  title?: string;
  busy?: boolean;
  canMutate?: boolean;
  onRename?: (item: ConversationSummary, title: string) => Promise<boolean>;
  onClear?: (item: ConversationSummary) => void;
  onDelete?: (item: ConversationSummary) => void;
}

export function HistoryDirectory({
  searchRef,
  mode,
  activeId,
  revision,
  onOpen,
  title,
  busy = false,
  canMutate = false,
  onRename,
  onClear,
  onDelete,
}: HistoryDirectoryProps): React.JSX.Element {
  const [items, setItems] = useState<ConversationSummary[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [notice, setNotice] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [query,setQuery]=useState("");
  const [titleQuery,setTitleQuery]=useState("");
  const operation = useRef<AbortController | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    operation.current?.abort();
    operation.current = controller;
    setItems([]);
    setNextCursor(null);
    setLoading(true);
    setNotice("");
    void listConversations(mode, controller.signal, undefined, titleQuery).then((page) => {
      if (!controller.signal.aborted) {
        setItems(page.items);
        setNextCursor(page.nextCursor);
      }
    }).catch((error: unknown) => {
      if (!controller.signal.aborted) setNotice(toPublicMessage(error));
    }).finally(() => {
      if (!controller.signal.aborted) setLoading(false);
    });
    return () => controller.abort();
  }, [mode, revision, refresh, titleQuery]);

  useEffect(() => () => operation.current?.abort(), []);

  useEffect(() => {
    setRenamingId(null);
    setDraft("");
  }, [mode, activeId]);

  async function more(): Promise<void> {
    if (!nextCursor || loading) return;
    const controller = new AbortController();
    operation.current?.abort();
    operation.current = controller;
    setLoading(true);
    setNotice("");
    try {
      const page = await listConversations(mode, controller.signal, nextCursor, titleQuery);
      if (!controller.signal.aborted) {
        setItems((previous) => [...previous, ...page.items.filter((item) => !previous.some((old) => old.id === item.id))]);
        setNextCursor(page.nextCursor);
      }
    } catch (error) {
      if (!controller.signal.aborted) setNotice(toPublicMessage(error));
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  }

  function startRename(item: ConversationSummary): void {
    setDraft(item.title ?? "");
    setRenamingId(item.id);
  }

  function closeRename(item: ConversationSummary): void {
    setRenamingId(null);
    document.querySelector<HTMLButtonElement>(`[data-history-menu="${item.id}"]`)?.focus();
  }

  return (
    <div className="history-directory" aria-busy={loading || busy}>
      <form className="history-search" onSubmit={event=>{event.preventDefault();setTitleQuery(query.trim());setRefresh(value=>value+1);}}>
        <label className="sr-only" htmlFor="historySearch">搜索历史标题</label>
        <input ref={searchRef} id="historySearch" value={query} maxLength={80} placeholder="搜索标题" onChange={event=>setQuery(event.target.value)} onKeyDown={event=>{if(event.key==="Enter"&&(event.nativeEvent.isComposing||event.keyCode===229))event.preventDefault();}} />
        <button className="history-refresh" type="submit" disabled={busy}>搜索</button>
      </form>
      <button className="history-refresh" type="button" disabled={loading} onClick={() => setRefresh((value) => value + 1)}>
        刷新目录
      </button>
      {notice && <p role="alert" className="coming-soon">{notice}</p>}
      {loading && <p role="status" className="coming-soon">正在加载目录</p>}
      {!loading && !notice && items.length === 0 && <p className="coming-soon">{titleQuery?"没有匹配标题的对话":"还没有此模式的对话"}</p>}
      <ul>
        {items.map((item) => {
          const active = item.id === activeId;
          return (
            <li key={item.id}>
              <div className="history-item" data-active={active ? "true" : undefined}>
                {renamingId === item.id && onRename ? (
                  <form
                    className="history-rename"
                    onSubmit={(event) => {
                      event.preventDefault();
                      const next = draft.trim();
                      if (!next) return;
                      void onRename(item, next).then((saved) => { if (saved) closeRename(item); });
                    }}
                  >
                    <label className="sr-only" htmlFor="conversationTitle">会话标题</label>
                    <input
                      id="conversationTitle"
                      autoFocus
                      disabled={busy}
                      value={draft}
                      onChange={(event) => setDraft(event.target.value)}
                      minLength={1}
                      maxLength={80}
                      required
                    />
                    <div className="history-rename-actions">
                      <button type="submit" disabled={busy}>保存</button>
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => {
                          if (active) setRenamingId(null);
                          else closeRename(item);
                        }}
                      >取消</button>
                    </div>
                  </form>
                ) : (
                  <>
                    <button
                      className="history-open"
                      type="button"
                      aria-current={active ? "true" : undefined}
                      onClick={() => onOpen(item.id)}
                    >
                      <span>{item.title || "新对话"}</span>
                      <small>{new Date(item.updated_at).toLocaleDateString()}</small>
                    </button>
                    {canMutate && (
                      <RowMenu
                        label={`会话操作 ${item.title || title || "新对话"}`}
                        triggerData={{ "data-history-menu": item.id }}
                        items={[
                          { label: "重命名", disabled: busy, onSelect: () => startRename(item) },
                          { label: "清空内容", disabled: busy, onSelect: () => onClear?.(item) },
                          { label: "删除会话", danger: true, disabled: busy, onSelect: () => onDelete?.(item) },
                        ]}
                      />
                    )}
                  </>
                )}
              </div>
            </li>
          );
        })}
      </ul>
      {nextCursor && (
        <button className="history-refresh" type="button" disabled={loading} onClick={() => void more()}>
          加载更多对话
        </button>
      )}
    </div>
  );
}
