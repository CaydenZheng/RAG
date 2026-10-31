import { useEffect, useRef, useState } from "react";
import { response } from "../api/workspaceClient";
import { toPublicMessage } from "../api/errors";
import type { ConversationTurn } from "../app/conversationState";
import { IS_PAGES_DEMO } from "../app/runtime";

export function ConversationExport({id,disabled,turns}: {id:string; disabled:boolean; turns:ConversationTurn[]}): React.JSX.Element {
  const [busy,setBusy]=useState(false),[notice,setNotice]=useState("");
  const controller=useRef<AbortController | null>(null);
  const urls=useRef(new Set<string>());
  useEffect(()=>()=>{controller.current?.abort();for(const url of urls.current)URL.revokeObjectURL(url);},[]);
  async function download(): Promise<void> {
    if(disabled||busy)return;
    const operation=new AbortController();controller.current=operation;setBusy(true);setNotice("");
    try{
      let blob: Blob;
      if(IS_PAGES_DEMO){
        const lines=turns.filter(turn=>turn.status==="completed").map(turn=>`${turn.question}\n\n${turn.answer}`).join("\n\n");
        const longest=Math.max(3,...(lines.match(/`+/g)??[]).map(value=>value.length+1));const fence="`".repeat(longest);
        blob=new Blob([`# 预设模拟对话（无真实历史）\n\n${fence}text\n${lines}\n${fence}\n`],{type:"text/markdown;charset=utf-8"});
      }else{blob=await (await response(`/conversations/${encodeURIComponent(id)}/export`,operation.signal)).blob();}
      if(operation.signal.aborted)return;
      if(blob.size>2*1024*1024){setNotice("此会话超过 2 MiB 导出上限。");return;}
      const url=URL.createObjectURL(blob);urls.current.add(url);const link=document.createElement("a");link.href=url;link.download=IS_PAGES_DEMO?"simulated-conversation.md":"conversation.md";link.click();
      setTimeout(()=>{URL.revokeObjectURL(url);urls.current.delete(url);},1000);setNotice("已生成 Markdown 文件；仅含已完成问答，正文按原样文本导出。");
    }catch(error){if(!operation.signal.aborted)setNotice(toPublicMessage(error));}
    finally{if(!operation.signal.aborted)setBusy(false);}
  }
  return <div className="conversation-export"><button className="skills-action" disabled={disabled||busy||!turns.some(turn=>turn.status==="completed")} onClick={()=>void download()}>{busy?"正在导出…":IS_PAGES_DEMO?"导出演示对话":"导出此会话"}</button>{notice&&<span role="status">{notice}</span>}</div>;
}
