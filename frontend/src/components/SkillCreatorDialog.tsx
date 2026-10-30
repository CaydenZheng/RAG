import { useEffect, useRef, useState, type KeyboardEvent } from "react";

import { isAbortFailure, toPublicMessage } from "../api/errors";
import { createSkill, type CreatorResult, type ModelChoice, type ModelOption, type SkillItem } from "../api/workspaceClient";
import { IS_PAGES_DEMO } from "../app/runtime";
import { ModelSelect } from "./ModelSelect";

export function SkillCreatorDialog({ models, defaultModel, onUpload, onClose }: {
  models: ModelOption[];
  defaultModel: ModelChoice;
  onUpload: (file: File, signal: AbortSignal) => Promise<SkillItem>;
  onClose: (uploadUnconfirmed: boolean) => void;
}): React.JSX.Element {
  const dialog = useRef<HTMLDialogElement>(null);
  const closeButton = useRef<HTMLButtonElement>(null);
  const operation = useRef<AbortController | null>(null);
  const mounted = useRef(false);
  const [source, setSource] = useState<File | null>(null);
  const [model, setModel] = useState<ModelChoice>(defaultModel);
  const [phase, setPhase] = useState<"creating" | "uploading" | null>(null);
  const [preview, setPreview] = useState("");
  const [result, setResult] = useState<CreatorResult | null>(null);
  const [download, setDownload] = useState("");
  const [notice, setNotice] = useState("");
  const [status, setStatus] = useState("");

  useEffect(() => {
    mounted.current = true;
    if (!dialog.current?.open) dialog.current?.showModal();
    return () => { mounted.current = false; operation.current?.abort(); };
  }, []);

  useEffect(() => {
    if (!result) { setDownload(""); return undefined; }
    const url = URL.createObjectURL(new Blob([result.content], {type:"text/markdown;charset=utf-8"}));
    setDownload(url);
    return () => URL.revokeObjectURL(url);
  }, [result]);

  useEffect(() => {
    if (dialog.current?.open && !dialog.current.contains(document.activeElement)) closeButton.current?.focus();
  }, [phase]);

  function keepFocus(event: KeyboardEvent<HTMLDialogElement>): void {
    if (event.key !== "Tab") return;
    const controls = Array.from(event.currentTarget.querySelectorAll<HTMLElement>(
      'button:not(:disabled), select:not(:disabled), input:not(:disabled), a[href]',
    )).filter(element => element.getClientRects().length > 0);
    const first = controls[0];
    const last = controls.at(-1);
    if (event.shiftKey && document.activeElement === first && last) {
      event.preventDefault(); last.focus();
    } else if (!event.shiftKey && document.activeElement === last && first) {
      event.preventDefault(); first.focus();
    }
  }

  async function generate(): Promise<void> {
    if (!source || operation.current || !models.some(item => item.id === model && item.available)) return;
    const controller = new AbortController();
    operation.current = controller;
    setPhase("creating"); setNotice(""); setStatus(""); setPreview(""); setResult(null);
    try {
      const generated = await createSkill(source, model, crypto.randomUUID(), controller.signal, chunk => {
        if (mounted.current && !controller.signal.aborted) setPreview(value => value + chunk);
      });
      if (mounted.current && !controller.signal.aborted) {
        setResult(generated); setPreview(generated.content);
        setStatus(IS_PAGES_DEMO ? "模拟转换完成，未进行真实格式校验、上传或启用。" : "技能校验通过，尚未上传或启用。");
      }
    } catch (error) {
      if (mounted.current && !isAbortFailure(error, controller.signal)) setNotice(toPublicMessage(error));
    } finally {
      if (operation.current === controller) {
        operation.current = null;
        if (mounted.current) setPhase(null);
      }
    }
  }

  function stop(): void {
    operation.current?.abort(); operation.current = null;
    setPhase(null); setPreview(""); setResult(null);
    setStatus("已停止转换，未保存或启用技能。");
  }

  async function upload(): Promise<void> {
    if (!result || operation.current) return;
    const controller = new AbortController();
    operation.current = controller;
    setPhase("uploading"); setNotice(""); setStatus("");
    try {
      const saved = await onUpload(new File([result.content], result.filename, {type:"text/markdown"}), controller.signal);
      if (mounted.current && !controller.signal.aborted) setStatus(saved.enabled
        ? "技能已上传，当前启用；已保留原有状态。"
        : "技能已上传，当前关闭；请明确启用后使用。");
    } catch (error) {
      if (mounted.current && !isAbortFailure(error, controller.signal)) setNotice(toPublicMessage(error));
    } finally {
      if (operation.current === controller) {
        operation.current = null;
        if (mounted.current) setPhase(null);
      }
    }
  }

  return <dialog ref={dialog} className="skill-creator-dialog" aria-labelledby="skillCreatorTitle" onKeyDown={keepFocus} onClose={() => {
    operation.current?.abort(); onClose(phase === "uploading");
  }} onClick={event => { if (event.target === event.currentTarget) dialog.current?.close(); }}>
    <section className="skill-detail">
      <header className="skill-detail-head"><h2 id="skillCreatorTitle">从资料生成技能</h2></header>
      <div className="skill-detail-body">
        <p>支持 TXT、Markdown、文字型 PDF、DOCX，最多 10 MiB／200 页。适合短文档，扫描件暂不支持。</p>
        <p>草稿只在当前窗口保留；校验后可下载或明确上传，不自动启用。</p>
        {IS_PAGES_DEMO && <p>此处为预设模拟，不读取文件、不连接模型，刷新会重置。</p>}
        <form className="skill-creator-form" onSubmit={event => { event.preventDefault(); void generate(); }}>
          <ModelSelect id="creatorModel" items={models} value={model} disabled={phase !== null} onChange={setModel} />
          <label>导入原始资料<input type="file" accept=".txt,.md,.pdf,.docx" disabled={phase !== null}
            onChange={event => {
              setSource(event.target.files?.[0] ?? null);
              setResult(null); setPreview(""); setNotice(""); setStatus("");
            }} /></label>
          <div className="skill-creator-actions">
            <button className="skills-action" type="submit" disabled={phase !== null || !source || !models.some(item => item.id === model && item.available)}>开始整理</button>
            {phase === "creating" && <button className="skills-action" type="button" onClick={stop}>停止转换</button>}
          </div>
        </form>
        {notice && <p className="skills-notice" role="alert">{notice}</p>}
        <p role="status" aria-live="polite">{status || (phase === "creating" ? "正在解析并整理资料…" : phase === "uploading" ? "正在上传技能…" : "")}</p>
        {preview && <><p>{result ? IS_PAGES_DEMO ? "预设模拟技能文件" : "已校验的技能文件" : "转换预览，尚未校验。"}</p>
          <pre className="skill-content" aria-label="技能转换预览">{preview}</pre></>}
        {result && <div className="skill-creator-actions">
          {download && <a href={download} download={result.filename}>下载技能文件</a>}
          <button className="skills-action" type="button" disabled={phase !== null} onClick={() => void upload()}>上传此技能（新项默认关闭）</button>
        </div>}
      </div>
      <footer className="skill-detail-foot"><button ref={closeButton} type="button" onClick={() => dialog.current?.close()}>关闭生成窗口</button></footer>
    </section>
  </dialog>;
}
