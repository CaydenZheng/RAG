import { useState, type RefObject } from "react";
export function DemoHistory({searchRef,onExample}: {searchRef:RefObject<HTMLInputElement | null>;onExample:(text:string)=>void}): React.JSX.Element {
  const [query,setQuery]=useState("");
  const examples=["审批示例","表单示例","URL 示例"];
  return <div className="history-directory"><p className="coming-soon">预设示例目录，非个人历史</p><label className="sr-only" htmlFor="historySearch">搜索历史标题</label><input className="demo-history-search" id="historySearch" ref={searchRef} value={query} maxLength={80} onChange={event=>setQuery(event.target.value)} placeholder="筛选示例标题" />
    <ul>{examples.filter(text=>text.includes(query)).map(text=><li key={text}><button className="history-open" onClick={()=>onExample(text)}>{text}（模拟）</button></li>)}</ul>
  </div>;
}
