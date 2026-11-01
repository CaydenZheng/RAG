import { useState, type RefObject } from "react";
import { RowMenu } from "./RowMenu";

interface DemoEntry {
  id: string;
  title: string;
  cleared?: boolean;
}

const EXAMPLES: DemoEntry[] = [
  { id: "demo-approval", title: "审批示例" },
  { id: "demo-form", title: "表单示例" },
  { id: "demo-url", title: "URL 示例" },
];

export function DemoHistory({ searchRef, onExample }: {
  searchRef: RefObject<HTMLInputElement | null>;
  onExample: (text: string) => void;
}): React.JSX.Element {
  const [entries, setEntries] = useState<DemoEntry[]>(EXAMPLES);
  const [query, setQuery] = useState("");
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [notice, setNotice] = useState("");
  const visible = entries.filter((entry) => entry.title.includes(query));

  function closeRename(id: string): void {
    setRenamingId(null);
    document.querySelector<HTMLButtonElement>(`[data-history-menu="${id}"]`)?.focus();
  }

  return <div className="history-directory">
    <label className="sr-only" htmlFor="historySearch">搜索历史标题</label>
    <input className="demo-history-search" id="historySearch" ref={searchRef} value={query} maxLength={80}
      placeholder="筛选示例标题" onChange={(event) => setQuery(event.target.value)} />
    {notice && <p className="coming-soon" role="status">{notice}</p>}
    {!visible.length && <p className="coming-soon">没有匹配的示例</p>}
    <ul>
      {visible.map((entry) => <li key={entry.id}>
        <div className="history-item">
          {renamingId === entry.id ? (
            <form className="history-rename" onSubmit={(event) => {
              event.preventDefault();
              const next = draft.trim();
              if (!next) return;
              setEntries((previous) => previous.map((item) => item.id === entry.id ? { ...item, title: next } : item));
              setNotice("模拟重命名已生效，刷新会重置。");
              closeRename(entry.id);
            }}>
              <label className="sr-only" htmlFor="conversationTitle">会话标题</label>
              <input id="conversationTitle" autoFocus value={draft} maxLength={80} required
                onChange={(event) => setDraft(event.target.value)} />
              <div className="history-rename-actions">
                <button type="submit">保存</button>
                <button type="button" onClick={() => closeRename(entry.id)}>取消</button>
              </div>
            </form>
          ) : (
            <>
              <button className="history-open" type="button" aria-label={entry.title} onClick={() => {
                if (entry.cleared) { setNotice(`「${entry.title}」已清空（模拟），刷新恢复。`); return; }
                onExample(entry.title);
              }}>
                <span>{entry.title}</span>
                <small>{entry.cleared ? "已清空" : "示例"}</small>
              </button>
              <RowMenu
                label={`会话操作 ${entry.title}`}
                triggerData={{ "data-history-menu": entry.id }}
                items={[
                  { label: "重命名", onSelect: () => { setDraft(entry.title); setRenamingId(entry.id); } },
                  {
                    label: "清空内容",
                    onSelect: () => {
                      setEntries((previous) => previous.map((item) => item.id === entry.id ? { ...item, cleared: true } : item));
                      setNotice(`「${entry.title}」已清空（模拟），刷新恢复。`);
                    },
                  },
                  {
                    label: "删除会话",
                    danger: true,
                    onSelect: () => {
                      setEntries((previous) => previous.filter((item) => item.id !== entry.id));
                      setNotice("模拟会话已从示例目录移除，刷新会恢复。");
                    },
                  },
                ]}
              />
            </>
          )}
        </div>
      </li>)}
    </ul>
  </div>;
}
