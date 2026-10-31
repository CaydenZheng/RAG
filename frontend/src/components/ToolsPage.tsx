import { useEffect, useState } from "react";
import { readToolCatalog, type ToolCatalog } from "../api/toolClient";
import { toPublicMessage } from "../api/errors";
import { IS_PAGES_DEMO } from "../app/runtime";

const SERVER_STATUS: Readonly<Record<string,string>> = {available:"连接可用",ready:"连接就绪",starting:"正在连接",disabled:"已禁用",degraded:"连接降级",unavailable:"不可用",stopped:"已断开",connecting:"正在连接"};

export function ToolsPage({available, onExample}: {available: boolean; onExample: (text:string) => void}): React.JSX.Element {
  const [catalog,setCatalog] = useState<ToolCatalog | null>(null);
  const [notice,setNotice] = useState("");
  const [revision,setRevision] = useState(0);
  const [loading,setLoading] = useState(true);
  useEffect(()=>{
    if(!available)return undefined;
    const controller=new AbortController();setLoading(true);setNotice("");setCatalog(null);
    void readToolCatalog(controller.signal).then(value=>{if(!controller.signal.aborted)setCatalog(value);})
      .catch(error=>{if(!controller.signal.aborted)setNotice(toPublicMessage(error));})
      .finally(()=>{if(!controller.signal.aborted)setLoading(false);});
    return ()=>controller.abort();
  },[available,revision]);
  return <section className="skills-page" aria-label="工具与 MCP">
    <div className="skills-head"><div><h1>工具与 MCP</h1><p className="skills-sub">只读能力目录；可用表示已登记，不保证外部服务实际调用成功。执行仍经过后端权限、Schema、审批和审计。</p></div></div>
    {IS_PAGES_DEMO&&<p>预设模拟目录，不连接 Provider 或执行工具。</p>}
    {!available?<p>登录后可查看当前账号允许的工具。</p>:<>
      <button className="skills-action" disabled={loading} onClick={()=>setRevision(value=>value+1)}>重新读取工具目录</button>
      {loading&&<p role="status">正在读取工具目录…</p>}{notice&&<p role="alert">{notice}</p>}
      {catalog&&<>
        <h2>工具</h2>{!catalog.tools.length&&<p>当前账号没有可见工具。</p>}
        <ul className="knowledge-documents">{catalog.tools.map(tool=><li key={tool.name}><div><strong>{tool.name}</strong><p>{tool.description}</p><p className="skills-sub">来源：{tool.source==="native"?"内置":"MCP"} · Provider：{tool.provider??"本地"} · 分类：{tool.category} · {tool.available?"已登记可用":"当前不可用"}</p>
          {tool.source==="native"&&tool.available&&tool.name==="calculator"&&<button className="skills-action" onClick={()=>onExample("请使用计算器计算 (12 + 21) * 12，给出结果。")}>将计算示例填入 Agent</button>}
          {tool.source==="mcp"&&tool.available&&tool.provider==="workspace_demo"&&["confirm_preview","request_form","request_url"].some(name=>tool.name===`mcp__workspace_demo__${name}`)&&<button className="skills-action" onClick={()=>onExample(`请只调用工具 ${tool.name} 完成本地受控交互示例，等待我的审批或补充输入，不调用其他工具。`)}>填入受控交互示例</button>}
        </div></li>)}</ul>
        <h2>MCP Provider</h2>{!catalog.servers.length?<p>没有当前账号可见的 MCP Provider；这不代表已连接外部服务。</p>:<ul className="knowledge-documents">{catalog.servers.map(server=><li key={server.id}><div><strong>{server.id}</strong><p>{SERVER_STATUS[server.status]??"状态未确认"} · {server.transport} · {server.toolCount} 个已发现工具</p></div></li>)}</ul>}
        <p className="skills-sub">工具需要时会在当前任务显示审批或补充输入；URL 仅在明确点击后打开。此页不提供 Server 添加、编辑、启停或直接执行。</p>
      </>}
    </>}
  </section>;
}
