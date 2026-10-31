import type { ToolCatalog } from "../api/toolClient";
export async function readToolCatalog(signal:AbortSignal):Promise<ToolCatalog>{
  signal.throwIfAborted();return {tools:[{name:"calculator",description:"模拟计算器：仅填入预设示例，不执行计算。",source:"native",provider:null,category:"utility",available:true},
    {name:"demo.preview",description:"模拟审批、form 和 URL 交互；没有真实 MCP。",source:"mcp",provider:"demo",category:"example",available:true}],
    servers:[{id:"demo · 预设模拟",transport:"无真实连接",status:"ready",toolCount:1}]};
}
