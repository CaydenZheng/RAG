import { useEffect, useRef, useState } from "react";
import { readPreferences, savePreferences, type ModelCatalog, type ModelChoice, type WorkspacePreferences } from "../api/workspaceClient";
import { toPublicMessage } from "../api/errors";
import type { ThemePreference } from "../app/useTheme";
import { IS_PAGES_DEMO } from "../app/runtime";

export function WorkspaceSettings({models,onSaved,theme,onTheme,onReloadModels}: {models: ModelCatalog | null; onSaved: (choice:ModelChoice) => void; theme: ThemePreference; onTheme:(value:ThemePreference)=>void;onReloadModels?:()=>void}): React.JSX.Element {
  const [preferences,setPreferences]=useState<WorkspacePreferences | null>(null);
  const [choice,setChoice]=useState<ModelChoice>("flash");
  const [busy,setBusy]=useState(true);
  const [notice,setNotice]=useState("");
  const [revision,setRevision]=useState(0);
  const operation=useRef<AbortController | null>(null);
  useEffect(()=>{
    const controller=new AbortController();operation.current=controller;setBusy(true);setPreferences(null);setNotice("");
    void readPreferences(controller.signal).then(value=>{if(!controller.signal.aborted){setPreferences(value);setChoice(value.defaultModel);onSaved(value.defaultModel);}})
      .catch(error=>{if(!controller.signal.aborted)setNotice(toPublicMessage(error));})
      .finally(()=>{if(!controller.signal.aborted)setBusy(false);});
    return ()=>operation.current?.abort();
  },[onSaved,revision]);
  async function save(): Promise<void> {
    if(!preferences||busy)return;
    const controller=new AbortController();operation.current?.abort();operation.current=controller;setBusy(true);setNotice("");
    try{const saved=await savePreferences(preferences,choice,controller.signal);if(!controller.signal.aborted){setPreferences(saved);setChoice(saved.defaultModel);setNotice("默认模型已保存，仅用于没有会话选择的新对话。");onSaved(saved.defaultModel);}}
    catch{if(!controller.signal.aborted){setPreferences(null);setNotice("保存结果未确认，请重新读取设置核对；不会自动重试。");}}
    finally{if(!controller.signal.aborted)setBusy(false);}
  }
  return <section className="skills-page" aria-label="工作台设置"><div className="skills-head"><div><h1>工作台设置</h1><p className="skills-sub">只保留实际生效的简单偏好；技能统一在 Skills 页启停。</p></div></div>
    {IS_PAGES_DEMO&&<p>模拟设置仅在当前页面内存生效；主题偏好保存在本浏览器。</p>}
    {!models&&<p role="status">模型目录尚未确认。<button className="skills-action" onClick={onReloadModels}>重新读取模型目录</button></p>}
    <label className="settings-field">主题<select aria-label="主题" value={theme} onChange={event=>onTheme(event.target.value==="dark"?"dark":event.target.value==="light"?"light":"system")}><option value="system">跟随系统</option><option value="light">浅色</option><option value="dark">深色</option></select></label>
    <label className="settings-field">新对话默认模型<select aria-label="新对话默认模型" disabled={busy||!preferences||!models} value={choice} onChange={event=>setChoice(event.target.value==="pro"?"pro":"flash")}>
      {(models?.items??[]).map(item=><option value={item.id} key={item.id} disabled={!item.available}>{item.label}{item.available?"":"（未配置）"}</option>)}
    </select></label>
    <p className="skills-sub">优先级：本轮明确选择 → 会话已保存选择／最近模型 → 账号默认 → Flash。更改默认不改写已有会话或历史。</p>
    <div className="interaction-actions"><button disabled={busy||!preferences||!models?.items.some(item=>item.id===choice&&item.available)} onClick={()=>void save()}>保存默认模型</button><button disabled={busy} onClick={()=>setRevision(value=>value+1)}>重新读取设置</button></div>
    {busy&&<p role="status">正在核对设置…</p>}{notice&&<p role="status">{notice}</p>}
    <h2>快捷操作</h2><p>Ctrl／⌘ + Alt + I 聚焦输入；+ N 新建对话；+ H 搜索历史。输入法组词、AltGraph 和弹窗内不触发。新建保留已保存历史。</p>
  </section>;
}
