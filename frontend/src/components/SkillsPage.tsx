import { useEffect, useRef, useState } from "react";

import { deleteSkill, readSkill, readSkills, setSkill, uploadSkill, type ModelChoice, type ModelOption, type SkillItem } from "../api/workspaceClient";
import { isAbortFailure, toPublicMessage } from "../api/errors";
import { IS_PAGES_DEMO } from "../app/runtime";
import { RowMenu } from "./RowMenu";
import { SkillCreatorDialog } from "./SkillCreatorDialog";

export function SkillsPage({ available, models = [], defaultModel = "flash" }: {
  available: boolean; models?: ModelOption[]; defaultModel?: ModelChoice;
}): React.JSX.Element {
  const [items, setItems] = useState<SkillItem[]>([]);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [status, setStatus] = useState("");
  const [selected, setSelected] = useState<SkillItem | null>(null);
  const [creatorOpen, setCreatorOpen] = useState(false);
  const operation = useRef<AbortController | null>(null);
  const active = useRef(false);
  const heading = useRef<HTMLHeadingElement>(null);
  const detailClose = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!available) return undefined;
    const controller = new AbortController();
    operation.current = controller;
    active.current = true;
    setBusy(true);
    void readSkills(controller.signal).then(values => {
      if (!controller.signal.aborted) setItems(values);
    }).catch(error => {
      if (!controller.signal.aborted) setNotice(toPublicMessage(error));
    }).finally(() => {
      if (operation.current === controller) {
        active.current = false;
        setBusy(false);
        operation.current = null;
      }
    });
    return () => {
      controller.abort();
      operation.current?.abort();
      operation.current = null;
      active.current = false;
    };
  }, [available]);

  useEffect(() => {
    if (selected) detailClose.current?.focus();
  }, [selected]);

  async function perform(action: (signal: AbortSignal) => Promise<void>): Promise<void> {
    if (!available || active.current) return;
    const controller = new AbortController();
    operation.current = controller;
    active.current = true;
    setBusy(true);
    setNotice("");
    setStatus("");
    try {
      await action(controller.signal);
    } catch (error) {
      if (!isAbortFailure(error, controller.signal)) setNotice(toPublicMessage(error));
    } finally {
      if (operation.current === controller) {
        operation.current = null;
        active.current = false;
        setBusy(false);
      }
    }
  }

  async function refresh(signal: AbortSignal): Promise<void> {
    const values = await readSkills(signal);
    if (!signal.aborted) setItems(values);
  }

  async function saveUpload(file: File, signal: AbortSignal): Promise<SkillItem> {
    try {
      const saved = await uploadSkill(file, signal);
      if (signal.aborted) throw new DOMException("Aborted", "AbortError");
      await refresh(signal);
      if (!signal.aborted) setStatus(saved.enabled
        ? "技能已上传，当前启用；已保留原有状态。"
        : "技能已上传，当前关闭；请明确启用后使用。");
      return saved;
    } catch (error) {
      if (!isAbortFailure(error, signal)) setNotice(toPublicMessage(error));
      throw error;
    }
  }

  function openDetail(item: SkillItem): void {
    void perform(async signal => {
      const detail = await readSkill(item.id, signal);
      if (!signal.aborted) setSelected(detail);
    });
  }

  function closeDetail(): void {
    const id = selected?.id;
    setSelected(null);
    if (id) document.querySelector<HTMLButtonElement>(`[data-skill-menu="${id}"]`)?.focus();
  }

  function remove(item: SkillItem): void {
    if (!window.confirm(`删除技能 ${item.name}？后续请求将不再使用它。`)) return;
    void perform(async signal => {
      await deleteSkill(item.id, signal);
      await refresh(signal);
      if (!signal.aborted) {
        setSelected(null);
        setStatus("技能已删除；已完成历史保持原样。");
        heading.current?.focus();
      }
    });
  }

  if (!available) {
    return <section className="skills-page" aria-label="技能管理">
      <div className="skills-head"><div><h1>技能</h1><p className="skills-sub">启用账号功能并登录后，可管理自己的技能。</p></div></div>
    </section>;
  }

  return <section className="skills-page" aria-label="技能管理">
    <div className="skills-head">
      <div>
        <h1 ref={heading} tabIndex={-1}>技能</h1>
        <p className="skills-sub">对 Agent 生效，不影响知识问答；开关只影响后续请求。</p>
      </div>
      <div className="skill-creator-actions">
      <button className="skills-action" type="button" disabled={busy || !items.some(item => item.id === "skill-creator" && item.enabled)}
        aria-describedby="creatorAvailability" onClick={() => setCreatorOpen(true)}>从资料生成技能</button>
      <label className="skills-action">
        上传技能
        <input
          type="file"
          accept=".md"
          disabled={busy}
          aria-label="上传技能"
          onChange={(event) => {
            const file = event.target.files?.[0];
            event.target.value = "";
            if (file) void perform(async signal => { await saveUpload(file, signal); });
          }}
        />
      </label>
      </div>
    </div>
    <p className="skills-sub creator-availability" id="creatorAvailability">启用 skill-creator 后，可从原始资料生成技能文件。</p>

    {IS_PAGES_DEMO && <p className="skills-sub">模拟数据，不连接模型，刷新会重置。</p>}
    {notice && <div className="skills-notice" role="alert"><p>{notice}</p>
      <button className="skills-action" type="button" disabled={busy} onClick={() => void perform(refresh)}>刷新目录</button>
    </div>}
    <p className="skills-status" role="status" aria-live="polite">{status || (busy ? "操作中，请稍候…" : "")}</p>

    <ul className="skill-rows" aria-busy={busy}>
      {items.map(item => (
        <li key={item.id} className="skill-row">
          <span className="skill-name">{item.name}</span>
          <span className="skill-desc">{item.description}</span>
          <label className="skill-switch">
            <span className="sr-only">启用 {item.name}</span>
            <input
              type="checkbox"
              checked={item.enabled}
              disabled={busy}
              onChange={(event) => {
                const enabled = event.target.checked;
                void perform(async signal => {
                  const saved = await setSkill(item, enabled, signal);
                  if (!signal.aborted) {
                    setItems(previous => previous.map(value => value.id === saved.id ? saved : value));
                    setStatus(`${saved.name} 已${saved.enabled ? "启用" : "关闭"}，后续请求生效。`);
                  }
                });
              }}
            />
            <span className="track" />
          </label>
          <RowMenu
            className="row-more"
            label={`更多操作 ${item.name}`}
            triggerData={{ "data-skill-menu": item.id }}
            items={[
              { label: "详情", disabled: busy, onSelect: () => openDetail(item) },
              ...(item.builtin ? [] : [{ label: "删除", danger: true, disabled: busy, onSelect: () => remove(item) }]),
            ]}
          />
        </li>
      ))}
    </ul>

    {selected && (
      <div
        className="skill-detail-bg"
        onPointerDown={(event) => { if (event.target === event.currentTarget) closeDetail(); }}
      >
        <section className="skill-detail" role="region" aria-label="技能详情">
          <header className="skill-detail-head">
            <h3>{selected.name}</h3>
            <span className="tag">{selected.builtin ? "内置" : "自定义"}</span>
          </header>
          <div className="skill-detail-body">
            <p>{selected.description}</p>
            <small>版本 {selected.version.slice(0, 12)}</small>
            <pre className="skill-content">{selected.content}</pre>
          </div>
          <div className="skill-detail-foot">
            <button ref={detailClose} type="button" disabled={busy} onClick={closeDetail}>关闭详情</button>
          </div>
        </section>
      </div>
    )}
    {creatorOpen && <SkillCreatorDialog models={models} defaultModel={defaultModel} onUpload={saveUpload} onClose={uploadUnconfirmed => {
      setCreatorOpen(false);
      if (uploadUnconfirmed) setNotice("上传结果未确认，请刷新目录核对。");
    }} />}
  </section>;
}
