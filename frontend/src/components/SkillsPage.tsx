import { useEffect, useRef, useState } from "react";

import { createSkill, deleteSkill, readSkill, readSkills, setSkill, uploadSkill, type CreatorResult, type ModelChoice, type ModelOption, type SkillItem } from "../api/workspaceClient";
import { isAbortFailure, toPublicMessage } from "../api/errors";
import { IS_PAGES_DEMO } from "../app/runtime";
import { ModelSelect } from "./ModelSelect";

export function SkillsPage({ available, models, defaultModel }: { available: boolean; models: ModelOption[]; defaultModel: ModelChoice }): React.JSX.Element {
  const [items, setItems] = useState<SkillItem[]>([]);
  const [selected, setSelected] = useState<SkillItem | null>(null);
  const [busy, setBusy] = useState(false);
  const [creating, setCreating] = useState(false);
  const [notice, setNotice] = useState("");
  const [status, setStatus] = useState("");
  const [upload, setUpload] = useState<File | null>(null);
  const [source, setSource] = useState<File | null>(null);
  const [model, setModel] = useState<ModelChoice>(defaultModel);
  const [preview, setPreview] = useState("");
  const [result, setResult] = useState<CreatorResult | null>(null);
  const [download, setDownload] = useState("");
  const operation = useRef<AbortController | null>(null);
  const active = useRef(false);
  const heading = useRef<HTMLHeadingElement>(null);
  const detailTrigger = useRef<HTMLButtonElement | null>(null);
  const creator = items.find(item => item.id === "skill-creator");

  useEffect(() => {
    if (!available) return undefined;
    const controller = new AbortController(); operation.current = controller;
    active.current = true; setBusy(true);
    void readSkills(controller.signal).then(values => {
      if (!controller.signal.aborted) setItems(values);
    }).catch(error => { if (!controller.signal.aborted) setNotice(toPublicMessage(error)); })
      .finally(() => { if (operation.current === controller) {active.current=false;setBusy(false);operation.current=null;} });
    return () => { controller.abort(); operation.current?.abort(); operation.current=null;active.current=false; };
  }, [available]);

  useEffect(() => {
    if (!result || typeof URL.createObjectURL !== "function") {setDownload("");return undefined;}
    const url = URL.createObjectURL(new Blob([result.content],{type:"text/markdown;charset=utf-8"}));
    setDownload(url);
    return () => URL.revokeObjectURL(url);
  }, [result]);

  async function perform(action: (signal: AbortSignal) => Promise<void>): Promise<void> {
    if (!available || active.current) return;
    const controller = new AbortController(); operation.current=controller;
    active.current=true;setBusy(true);setNotice("");setStatus("");
    try { await action(controller.signal); }
    catch(error) { if (!isAbortFailure(error,controller.signal)) setNotice(toPublicMessage(error)); }
    finally { if (operation.current===controller) {operation.current=null;active.current=false;setBusy(false);} }
  }

  async function refresh(signal: AbortSignal): Promise<void> {
    const values = await readSkills(signal);
    if (!signal.aborted) setItems(values);
  }

  async function saveUpload(file: File, signal: AbortSignal): Promise<void> {
    const saved = await uploadSkill(file, signal);
    await refresh(signal);
    if (!signal.aborted) setStatus(saved.enabled
      ? "技能已上传，当前启用；已保留原有状态。"
      : "技能已上传，当前关闭；请明确启用后使用。");
  }

  function cancel(): void {
    operation.current?.abort();operation.current=null;active.current=false;
    setBusy(false);setCreating(false);setResult(null);setStatus("已停止转换，未保存或启用技能。");
  }

  if (!available) return <section className="skills-page"><h2>Skills</h2><p>启用账号功能并登录后，可管理自己的技能。</p></section>;
  return <section className="skills-page" aria-label="技能管理">
    <h2 ref={heading} tabIndex={-1}>Skills 技能</h2>
    {IS_PAGES_DEMO && <p className="demo-notice">以下为内存中的模拟技能；不连接模型、不执行工具，刷新会重置。</p>}
    <p>开关影响后续 Agent 请求；当前运行保留开始时的快照。技能不增加工具权限，知识问答不加载技能。</p>
    <button type="button" disabled={busy} onClick={() => void perform(refresh)}>刷新技能列表</button>
    {notice && <p className="page-error" role="alert">{notice}</p>}
    <p role="status" aria-live="polite">{status || (busy ? "操作中，请稍候…" : "")}</p>
    <ul className="skill-list" aria-busy={busy}>
      {items.map(item => <li key={item.id} className="skill-card">
        <div><strong>{item.name}</strong><span className="soon-badge">{item.builtin ? "内置" : "自定义"}</span></div>
        <p>{item.description}</p>
        <label><input type="checkbox" checked={item.enabled} disabled={busy} aria-label={`启用 ${item.name}`} onChange={(event) => {
          const enabled=event.target.checked;
          void perform(async signal => {const saved=await setSkill(item,enabled,signal);if(!signal.aborted){setItems(previous=>previous.map(value=>value.id===saved.id?saved:value));setStatus(`${saved.name} 已${saved.enabled?"启用":"关闭"}，后续请求生效。`);}});
        }} />启用</label>
        <button type="button" disabled={busy} aria-label={`查看 ${item.name}`} onClick={event => {
          detailTrigger.current = event.currentTarget;
          void perform(async signal=>{const detail=await readSkill(item.id,signal);if(!signal.aborted)setSelected(detail);});
        }}>查看详情</button>
        {!item.builtin && <button type="button" className="danger-button" disabled={busy} aria-label={`删除 ${item.name}`} onClick={()=>{
          if(!window.confirm(`删除技能 ${item.name}？后续请求将不再使用它。`))return;
          void perform(async signal=>{await deleteSkill(item.id,signal);await refresh(signal);if(!signal.aborted){setSelected(null);setStatus("技能已删除；已完成历史保持原样。");heading.current?.focus();}});
        }}>删除</button>}
      </li>)}
    </ul>
    {selected && <section aria-label="技能详情"><h3>{selected.name}</h3><p>版本：{selected.version.slice(0,12)}</p><pre className="skill-content">{selected.content}</pre><button type="button" disabled={busy} onClick={()=>{setSelected(null);detailTrigger.current?.focus();}}>关闭详情</button></section>}
    <section className="skill-card" aria-labelledby="uploadSkillTitle">
      <h3 id="uploadSkillTitle">上传已合规技能</h3>
      <p>UTF-8 单文件 Markdown，最多 64 KiB。元数据仅 schema_version、name、description；正文最多 8000 字。新技能默认关闭，重复上传保留现有启用状态。</p>
      <details><summary>查看格式示例</summary><pre className="skill-content">{"---\nschema_version: 1\nname: concise-writing\ndescription: 帮助精简表达\n---\n先给结论，再提供必要依据。"}</pre></details>
      <label>选择技能文件<input type="file" accept=".md" disabled={busy} onChange={event=>setUpload(event.target.files?.[0]??null)} /></label>
      <button type="button" disabled={busy||!upload} onClick={()=>{if(upload)void perform(signal=>saveUpload(upload,signal));}}>上传技能</button>
    </section>
    <section className="skill-card" aria-labelledby="creatorTitle">
      <h3 id="creatorTitle">整理为技能</h3>
      <p>先启用 skill-creator，再导入短文档。支持 TXT、MD、文字型 PDF、DOCX，最多 10 MiB／200 页；解析保护上限不代表全文能放入上下文。</p>
      <p>生成草稿仅在当前页面保留，刷新或离开会丢失；通过校验后可下载或明确上传，不自动安装、启用或执行。</p>
      <ModelSelect id="creatorModel" items={models} value={model} disabled={busy} onChange={setModel} />
      <label>导入原始资料<input type="file" accept=".txt,.md,.pdf,.docx" disabled={busy||!creator?.enabled} onChange={event=>setSource(event.target.files?.[0]??null)} /></label>
      <button type="button" disabled={busy||!creator?.enabled||!source||!models.some(item=>item.id===model&&item.available)} onClick={()=>{
        if(!source)return;
        void perform(async signal=>{
          setPreview("");setResult(null);setCreating(true);
          try {
            const generated=await createSkill(source,model,crypto.randomUUID(),signal,chunk=>{if(!signal.aborted)setPreview(value=>value+chunk);});
            if(!signal.aborted){setResult(generated);setPreview(generated.content);setStatus(IS_PAGES_DEMO?"模拟转换完成，未进行真实格式校验、上传或启用。":"技能格式与完整性校验通过，尚未上传或启用。");}
          } finally { if(!signal.aborted)setCreating(false); }
        });
      }}>开始整理</button>
      {creating&&<button type="button" onClick={cancel}>停止转换</button>}
      {preview&&<><p>{result?(IS_PAGES_DEMO?"预设模拟技能文件":"已校验的技能文件"):"转换预览，尚未校验，不可作为成功产物。"}</p><pre className="skill-content" aria-label="技能转换预览">{preview}</pre></>}
      {result&&<div className="skill-result">
        {download&&<a href={download} download={result.filename}>下载技能文件</a>}
        <button type="button" disabled={busy} onClick={()=>void perform(signal=>saveUpload(
          new File([result.content],result.filename,{type:"text/markdown"}),signal
        ))}>上传此技能（新项默认关闭）</button>
      </div>}
    </section>
  </section>;
}
