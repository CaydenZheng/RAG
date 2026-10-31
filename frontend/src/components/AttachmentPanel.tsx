import { useEffect, useRef, useState, type DragEvent } from "react";
import { Paperclip } from "lucide-react";
import { deleteAttachment, downloadAttachment, readAttachments, uploadAttachment, type AttachmentItem } from "../api/resourceClient";
import { toPublicMessage } from "../api/errors";
import { IS_PAGES_DEMO } from "../app/runtime";

const FAILURES: Readonly<Record<string, string>> = Object.freeze({
  parse_interrupted:"解析因服务重启中断，请删除后重新上传。", document_timeout:"解析超时，请拆分文件。",
  document_too_large:"文件或提取文本超过限制，请拆分。", document_busy:"解析繁忙，请删除后重新上传。",
});

export function AttachmentPanel({sessionId, exists, disabled, ensure, onSelection, onDeleted}: {
  sessionId: string; exists: boolean; disabled: boolean;
  ensure: (signal: AbortSignal) => Promise<void>;
  onSelection: (ids: string[], pending: boolean) => void;
  onDeleted?: (id: string) => void;
}): React.JSX.Element {
  const [items, setItems] = useState<AttachmentItem[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [revision, setRevision] = useState(0);
  const [retry, setRetry] = useState<{file:File; id:string} | null>(null);
  const operation = useRef<AbortController | null>(null);
  const mounted = useRef(true);
  const urls = useRef(new Set<string>());

  useEffect(() => { mounted.current = true; return () => {
    mounted.current = false; operation.current?.abort();
    for (const url of urls.current) URL.revokeObjectURL(url);
  }; }, []);

  useEffect(() => {
    onSelection(selected, busy || selected.some(id => !items.some(item => item.id === id && item.state === "ready")));
  }, [selected, items, busy, onSelection]);

  useEffect(() => {
    if (!exists) return undefined;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function refresh(): Promise<void> {
      try {
        const values = await readAttachments(sessionId, controller.signal);
        if (controller.signal.aborted) return;
        setItems(values); setSelected(previous => previous.filter(id => values.some(item => item.id === id && item.state !== "failed")));
        if (values.some(item => item.state === "parsing")) timer = setTimeout(() => void refresh(), 600);
      } catch (error) { if (!controller.signal.aborted) setNotice(toPublicMessage(error)); }
    }
    void refresh();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [exists, sessionId, revision]);

  async function upload(file: File, id: string = crypto.randomUUID()): Promise<void> {
    if (disabled || operation.current) return;
    if (file.size > 10 * 1024 * 1024 || !/\.(txt|md|pdf|docx)$/i.test(file.name)) {
      setNotice("仅支持 10 MiB 以内的 TXT、Markdown、文字型 PDF、DOCX。"); return;
    }
    const controller = new AbortController(); operation.current = controller;
    setBusy(true); setNotice(""); setRetry(null);
    try {
      await ensure(controller.signal);
      if (controller.signal.aborted) return;
      const item = await uploadAttachment(sessionId, id, file, controller.signal);
      if (controller.signal.aborted) return;
      setItems(previous => [...previous.filter(old => old.id !== item.id), item]);
      if (item.state !== "failed") setSelected(previous => [...new Set([...previous, item.id])]);
      setRevision(value => value + 1);
    } catch (error) {
      if (!controller.signal.aborted) {
        setNotice(`${toPublicMessage(error)} 上传结果可能未确认；重试会核对同一次上传。`);
        setRetry({file, id});
      }
    } finally {
      if (operation.current === controller) {
        operation.current = null;
        if (mounted.current) setBusy(false);
      }
    }
  }

  async function act(item: AttachmentItem, remove: boolean): Promise<void> {
    if (disabled || operation.current) return;
    if (remove && !window.confirm(`删除附件 ${item.name}？已完成的历史回答保留。`)) return;
    const controller = new AbortController(); operation.current = controller; setBusy(true); setNotice("");
    try {
      if (remove) {
        await deleteAttachment(sessionId, item.id, controller.signal);
        if (!controller.signal.aborted) {
          setItems(previous => previous.filter(old => old.id !== item.id)); setSelected(previous => previous.filter(id => id !== item.id));
          onDeleted?.(item.id);
        }
      } else {
        const blob = await downloadAttachment(sessionId, item.id, controller.signal);
        if (controller.signal.aborted) return;
        const url = URL.createObjectURL(blob); urls.current.add(url);
        const link = document.createElement("a"); link.href = url; link.download = item.name; link.click();
        setTimeout(() => { URL.revokeObjectURL(url); urls.current.delete(url); }, 1000);
      }
      if (!controller.signal.aborted) setRevision(value => value + 1);
    } catch (error) { if (!controller.signal.aborted) setNotice(toPublicMessage(error)); }
    finally { if (operation.current === controller) { operation.current = null; if (mounted.current) setBusy(false); } }
  }

  function drop(event: DragEvent<HTMLElement>): void {
    event.preventDefault();
    const file = event.dataTransfer.files[0];
    if (event.dataTransfer.files.length > 1) { setNotice("请逐个上传文件，每个会话最多 5 个。"); return; }
    if (file) void upload(file);
  }

  return <section className="attachment-panel" aria-label="会话附件" onDragOver={event => event.preventDefault()} onDrop={drop}>
    <div className="attachment-heading"><label className="skills-action"><Paperclip size={15} aria-hidden="true" />添加附件
      <input type="file" aria-label="添加聊天附件" accept=".txt,.md,.pdf,.docx" disabled={disabled || busy} onChange={event => {
        const file = event.target.files?.[0]; event.target.value = ""; if (file) void upload(file);
      }} /></label><span>{busy ? "正在上传／操作…" : `${items.length}/5 个文件，可拖入此处`}</span>
      <button className="skills-action" disabled={busy || disabled} type="button" onClick={() => {setNotice(""); setRevision(value => value + 1);}}>刷新附件</button></div>
    <p className="attachment-hint">适合短文档；单文件最多 10 MiB，PDF 最多 200 页仅为解析保护。全文不截断，输出额度受剩余上下文限制，不足时需拆分。扫描件暂不支持。</p>
    {IS_PAGES_DEMO && <p className="attachment-hint">预设模拟，不读取文件内容，不连接后端；刷新会重置。</p>}
    {notice && <p role="alert">{notice}</p>}
    {retry && <button type="button" className="skills-action" disabled={disabled || busy} onClick={() => void upload(retry.file, retry.id)}>核对／重试本次上传</button>}
    <ul className="attachment-list">{items.map(item => <li key={item.id}>
      <label><input type="checkbox" checked={selected.includes(item.id)} disabled={disabled || busy || item.state !== "ready"} onChange={event => {
        setSelected(previous => event.target.checked ? [...previous, item.id] : previous.filter(id => id !== item.id));
      }} />本轮使用 {item.name}</label>
      <span role="status">{item.state === "ready" ? `解析完成 · ${item.bytes} 字节${item.page_count ? ` · ${item.page_count} 页` : ""}` : item.state === "parsing" ? "正在解析…" : item.error_code && Object.hasOwn(FAILURES, item.error_code) ? FAILURES[item.error_code] : "解析失败或没有可提取文字，请删除后重新上传。"}</span>
      <button type="button" className="skills-action" disabled={disabled || busy || item.state !== "ready"} onClick={() => void act(item, false)}>下载 {item.name}</button>
      <button type="button" className="skills-action" disabled={disabled || busy} onClick={() => void act(item, true)}>删除 {item.name}</button>
    </li>)}</ul>
  </section>;
}
