import { useState, type RefObject } from "react";

import type { WorkspaceMode } from "../api/types";
import type { LocalConversation } from "../app/useLocalArchive";
import { HistorySearch } from "./HistorySearch";
import { RowMenu } from "./RowMenu";
import { useConfirm } from "./ConfirmDialog";

export interface LocalExample {
  id: string;
  title: string;
}

interface LocalHistoryProps {
  mode: WorkspaceMode;
  entries: readonly LocalConversation[];
  examples?: readonly LocalExample[];
  activeId: string | null;
  searchRef?: RefObject<HTMLInputElement | null>;
  onOpen: (entry: LocalConversation) => void;
  onExample?: (text: string) => void;
  onDelete: (id: string) => void;
  onRename: (id: string, title: string) => void;
}

export function LocalHistory({
  mode,
  entries,
  examples = [],
  activeId,
  searchRef,
  onOpen,
  onExample,
  onDelete,
  onRename,
}: LocalHistoryProps): React.JSX.Element {
  const [query, setQuery] = useState("");
  const [applied, setApplied] = useState("");
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [notice, setNotice] = useState("");
  const confirm = useConfirm();

  const all = [
    ...entries.map((entry) => ({ kind: "local" as const, id: entry.id, title: entry.title, entry })),
    ...examples.map((example) => ({ kind: "example" as const, id: example.id, title: example.title, entry: null })),
  ];
  const visible = all.filter((item) => item.title.includes(applied));

  function closeRename(id: string): void {
    setRenamingId(null);
    document.querySelector<HTMLButtonElement>(`[data-history-menu="${id}"]`)?.focus();
  }

  return <div className="history-directory">
    <HistorySearch
      inputRef={searchRef}
      value={query}
      placeholder="搜索标题"
      onChange={setQuery}
      onSubmit={() => setApplied(query.trim())}
      onClear={() => { setQuery(""); setApplied(""); }}
      onRefresh={() => { setQuery(""); setApplied(""); setNotice(""); }}
      refreshLabel="重置本地列表"
    />
    {notice && <p className="coming-soon" role="status">{notice}</p>}
    {!visible.length && <p className="coming-soon">{applied ? "没有匹配的标题" : mode === "agent" ? "还没有本地对话" : "还没有本地对话"}</p>}
    <ul>
      {visible.map((item) => <li key={item.id}>
        {renamingId === item.id && item.kind === "local" ? (
          <div className="history-item">
            <form className="history-rename" onSubmit={(event) => {
              event.preventDefault();
              const next = draft.trim();
              if (!next) return;
              onRename(item.id, next);
              closeRename(item.id);
            }}>
              <label className="sr-only" htmlFor="conversationTitle">会话标题</label>
              <input id="conversationTitle" autoFocus value={draft} maxLength={80} required
                onChange={(event) => setDraft(event.target.value)} />
              <div className="history-rename-actions">
                <button type="submit">保存</button>
                <button type="button" onClick={() => closeRename(item.id)}>取消</button>
              </div>
            </form>
          </div>
        ) : (
          <div className="history-item" data-active={activeId === item.id ? "true" : undefined}>
            <button className="history-open" type="button" aria-label={item.title} onClick={() => {
              if (item.kind === "example") {
                if (!onExample) return;
                onExample(item.title);
                return;
              }
              onOpen(item.entry);
            }}>
              <span>{item.title}</span>
              <small>{item.kind === "example" ? "示例" : "本地"}</small>
            </button>
            {item.kind === "local" && (
              <RowMenu
                label={`会话操作 ${item.title}`}
                triggerData={{ "data-history-menu": item.id }}
                items={[
                  { label: "重命名", onSelect: () => { setDraft(item.title); setRenamingId(item.id); } },
                  {
                    label: "删除会话",
                    danger: true,
                    onSelect: () => {
                      void confirm({
                        title: "删除本地对话",
                        message: `删除「${item.title}」及历史？刷新后本就无法恢复。`,
                        confirmLabel: "删除",
                        danger: true,
                      }).then((confirmed) => { if (confirmed) onDelete(item.id); });
                    },
                  },
                ]}
              />
            )}
          </div>
        )}
      </li>)}
    </ul>
  </div>;
}
