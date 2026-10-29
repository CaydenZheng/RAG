import { useEffect, useRef, useState } from "react";
import { listConversations, type ConversationSummary } from "../api/conversationClient";
import { toPublicMessage } from "../api/errors";
import type { WorkspaceMode } from "../api/types";

interface HistoryDirectoryProps {
  mode: WorkspaceMode;
  activeId: string;
  revision: number;
  onOpen: (id: string) => void;
}

export function HistoryDirectory({ mode, activeId, revision, onOpen }: HistoryDirectoryProps): React.JSX.Element {
  const [items, setItems] = useState<ConversationSummary[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [busy, setBusy] = useState(true);
  const [notice, setNotice] = useState("");
  const [refresh, setRefresh] = useState(0);
  const operation = useRef<AbortController | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    operation.current?.abort(); operation.current = controller;
    setItems([]); setNextCursor(null); setBusy(true); setNotice("");
    void listConversations(mode,controller.signal).then((page) => {
      if (!controller.signal.aborted) { setItems(page.items); setNextCursor(page.nextCursor); }
    }).catch((error: unknown) => { if (!controller.signal.aborted) setNotice(toPublicMessage(error)); })
      .finally(() => { if (!controller.signal.aborted) setBusy(false); });
    return () => controller.abort();
  }, [mode,revision,refresh]);

  useEffect(() => () => operation.current?.abort(), []);

  async function more(): Promise<void> {
    if (!nextCursor || busy) return;
    const controller = new AbortController(); operation.current?.abort(); operation.current = controller;
    setBusy(true); setNotice("");
    try {
      const page = await listConversations(mode,controller.signal,nextCursor);
      if (!controller.signal.aborted) {
        setItems((previous) => [...previous,...page.items.filter((item) => !previous.some((old) => old.id === item.id))]);
        setNextCursor(page.nextCursor);
      }
    } catch (error) { if (!controller.signal.aborted) setNotice(toPublicMessage(error)); }
    finally { if (!controller.signal.aborted) setBusy(false); }
  }

  return <div className="history-directory" aria-busy={busy}>
    <button type="button" disabled={busy} onClick={() => setRefresh((value) => value + 1)}>刷新目录</button>
    {notice && <p role="alert">{notice}</p>}
    {!busy && !notice && items.length === 0 && <p className="coming-soon">还没有此模式的对话</p>}
    {busy && <p role="status">正在加载目录</p>}
    <ul>{items.map((item) => <li key={item.id}>
      <button type="button" aria-current={activeId === item.id ? "true" : undefined} onClick={() => onOpen(item.id)}>
        <span>{item.title || "新对话"}</span><small>{new Date(item.updated_at).toLocaleDateString()}</small>
      </button>
    </li>)}</ul>
    {nextCursor && <button type="button" disabled={busy} onClick={() => void more()}>加载更多对话</button>}
  </div>;
}
