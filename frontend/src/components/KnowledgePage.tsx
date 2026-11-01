import { useEffect, useRef, useState } from "react";
import { readKnowledge, readKnowledgeExcerpt, type KnowledgeDocument, type KnowledgeExcerpt } from "../api/resourceClient";
import { toPublicMessage } from "../api/errors";
import { IS_PAGES_DEMO } from "../app/runtime";

export function KnowledgePage({available}: {available: boolean}): React.JSX.Element {
  const [items, setItems] = useState<KnowledgeDocument[]>([]);
  const [query, setQuery] = useState("");
  const [appliedQuery, setAppliedQuery] = useState("");
  const [next, setNext] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [version, setVersion] = useState("");
  const [excerpt, setExcerpt] = useState<KnowledgeExcerpt | null>(null);
  const operation = useRef<AbortController | null>(null);
  const heading = useRef<HTMLHeadingElement>(null);

  async function load(search: string, cursor?: string): Promise<void> {
    operation.current?.abort();
    const controller = new AbortController(); operation.current = controller;
    setBusy(true); setNotice(""); setExcerpt(null);
    try {
      const page = await readKnowledge(controller.signal, search, cursor);
      if (controller.signal.aborted) return;
      setItems(previous => cursor ? [...previous, ...page.items.filter(item => !previous.some(old => old.id === item.id))] : page.items);
      setAppliedQuery(search); setNext(page.next_cursor); setVersion(page.index_version);
    } catch (error) { if (!controller.signal.aborted) setNotice(toPublicMessage(error)); }
    finally { if (!controller.signal.aborted) setBusy(false); }
  }

  useEffect(() => {
    if (available) void load("");
    return () => operation.current?.abort();
  }, [available]);

  async function view(item: KnowledgeDocument): Promise<void> {
    operation.current?.abort();
    const controller = new AbortController(); operation.current = controller; setBusy(true); setNotice("");
    try {
      const value = await readKnowledgeExcerpt(item, controller.signal);
      if (!controller.signal.aborted) { setExcerpt(value); heading.current?.focus(); }
    } catch (error) { if (!controller.signal.aborted) setNotice(toPublicMessage(error)); }
    finally { if (!controller.signal.aborted) setBusy(false); }
  }

  return <section className="skills-page" aria-label="知识库浏览">
    <div className="skills-head">
      <div><h1 ref={heading} tabIndex={-1}>知识库</h1><p className="skills-sub">共享只读目录；聊天附件不会进入知识库。</p></div>
      {available && <div className="skill-creator-actions">
        <button className="skills-action plain" type="button" disabled={busy} onClick={() => void load(appliedQuery)}>刷新目录</button>
      </div>}
    </div>
    {!available ? <p className="page-note">登录后可浏览已索引文档。</p> : <>
            <form className="resource-search" onSubmit={event => {event.preventDefault(); void load(query.trim());}}>
        <label className="sr-only" htmlFor="knowledgeSearch">搜索文档名称</label>
        <input id="knowledgeSearch" value={query} maxLength={80} placeholder="搜索文档名称" onChange={event => setQuery(event.target.value)} />
        {query && <button className="history-clear" type="button" aria-label="清除搜索" onClick={() => setQuery("")}>✕</button>}
        <button className="skills-action" disabled={busy} type="submit">搜索</button>
      </form>
      <p className="skills-status" role="status">{busy ? "正在读取知识库…" : version ? `索引版本 ${version} · 已索引` : ""}</p>
      {notice && <p role="alert" className="skills-notice">{notice}</p>}
      {!busy && !notice && !items.length && <p className="page-note">没有匹配的文档。</p>}
      <ul className="knowledge-documents">{items.map(item => <li key={item.id}>
        <div><strong>{item.name}</strong><p className="field-hint">原文更新时间：{item.updated_at ?? "未提供"} · 索引时间：{item.indexed_at}</p></div>
        <button className="skills-action" type="button" disabled={busy} onClick={() => void view(item)}>查看来源摘录：{item.name}</button>
      </li>)}</ul>
      {next && <button className="skills-action" disabled={busy} onClick={() => void load(appliedQuery, next)}>加载更多文档</button>}
      {excerpt && <section aria-label="来源摘录"><h2 className="page-section">{excerpt.name}</h2><p className="page-note">以下仅为最多 20 个片段、20000 字符的索引摘录，不是原文全文。</p><pre className="skill-content">{excerpt.text}</pre></section>}
    </>}
  </section>;
}
