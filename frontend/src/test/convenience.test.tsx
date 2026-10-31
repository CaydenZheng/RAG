import { act, fireEvent, render, renderHook, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ToolsPage } from "../components/ToolsPage";
import { WorkspaceSettings } from "../components/WorkspaceSettings";
import { HistoryDirectory } from "../components/HistoryDirectory";
import { ConversationView } from "../components/ConversationView";
import { App } from "../app/App";
import { useTheme } from "../app/useTheme";
import { useConversation } from "../app/useConversation";
import type { ModelCatalog } from "../api/workspaceClient";
import { conversationReducer, initialConversationState } from "../app/conversationState";

const MODELS:ModelCatalog={items:[{id:"flash",label:"Flash",available:true},{id:"pro",label:"Pro",available:true}],defaultModel:"flash"};
const json=(value:unknown):Response=>new Response(JSON.stringify(value),{headers:{"Content-Type":"application/json"}});

describe("fourth unit public interactions",()=>{
  it("new conversations use the server account default instead of the old conversation selection",async()=>{
    let created:Record<string,unknown>|null=null;
    vi.stubGlobal("fetch",vi.fn((_:string,init?:RequestInit)=>{created=JSON.parse(String(init?.body));return Promise.resolve(json({...created,title:"",revision:0,epoch:0,model:"flash",updated_at:"2031-01-01T00:00:00Z"}));}));
    const {result}=renderHook(()=>useConversation("agent",true,"flash"));await act(async()=>result.current.chooseModel("pro"));expect(result.current.model).toBe("pro");await act(async()=>result.current.reset());expect(created).not.toHaveProperty("model");expect(result.current.model).toBe("flash");
  });
  it("projects tool and provider status without free connection messages or executing tools",async()=>{
    const user=userEvent.setup(),example=vi.fn();
    const fetch=vi.fn().mockResolvedValue(json({tools:[{name:"calculator",description:"数学计算",source:"native",provider:null,category:"utility",available:true}],mcp_servers:[{id:"example",transport:"stdio",status:"unavailable",tool_count:0,message:"HIDDEN_QUERY",url:"https://hidden.example/?token=PRIVATE"}]}));vi.stubGlobal("fetch",fetch);
    render(<ToolsPage available onExample={example}/>);
    expect(await screen.findByText("calculator")).toBeVisible();expect(screen.getByText(/不可用 · stdio/)).toBeVisible();expect(document.body).not.toHaveTextContent("HIDDEN_QUERY");expect(document.body).not.toHaveTextContent("PRIVATE");
    await user.click(screen.getByRole("button",{name:"将计算示例填入 Agent"}));expect(example).toHaveBeenCalledWith("请使用计算器计算 (12 + 21) * 12，给出结果。");
    expect(fetch).toHaveBeenCalledTimes(1);expect(fetch.mock.calls[0][0]).toBe("/agent/tools");
  });
  it("locks an unconfirmed preference write until an explicit server reread",async()=>{
    const user=userEvent.setup();let stored="flash",revision=0;
    vi.stubGlobal("fetch",vi.fn((_:string,init?:RequestInit)=>{
      if(init?.method==="PATCH"){const body=JSON.parse(String(init.body));expect(body.expected_revision).toBe(0);stored=body.default_model;revision++;return Promise.reject(new TypeError("lost response"));}
      return Promise.resolve(json({default_model:stored,revision}));
    }));
    render(<WorkspaceSettings models={MODELS} onSaved={vi.fn()} theme="system" onTheme={vi.fn()}/>);
    const selector=screen.getByRole("combobox",{name:"新对话默认模型"});await waitFor(()=>expect(selector).toBeEnabled());await user.selectOptions(selector,"pro");await user.click(screen.getByRole("button",{name:"保存默认模型"}));
    expect(await screen.findByText(/保存结果未确认/)).toBeVisible();expect(selector).toBeDisabled();expect(screen.getByRole("button",{name:"保存默认模型"})).toBeDisabled();
    await user.click(screen.getByRole("button",{name:"重新读取设置"}));await waitFor(()=>expect(selector).toBeEnabled());expect(selector).toHaveValue("pro");
  });
  it("keeps theme preference as the only browser-persisted setting",async()=>{
    vi.stubGlobal("fetch",vi.fn().mockResolvedValue(json({default_model:"flash",revision:0})));
    function Harness():React.JSX.Element {const {theme,changeTheme}=useTheme();return <WorkspaceSettings models={MODELS} onSaved={vi.fn()} theme={theme} onTheme={changeTheme}/>;}
    const user=userEvent.setup();render(<Harness/>);await user.selectOptions(screen.getByRole("combobox",{name:"主题"}),"dark");expect(document.documentElement.dataset.theme).toBe("dark");expect(localStorage.getItem("ragflow_theme")).toBe("dark");await user.selectOptions(screen.getByRole("combobox",{name:"主题"}),"system");expect(document.documentElement.dataset.theme).toBe("system");expect(localStorage.length).toBe(1);
  });
  it("searches titles in POST bodies and retains the query for pagination",async()=>{
    const user=userEvent.setup();let searches=0;
    const fetch=vi.fn((path:string,init?:RequestInit)=>{
      if(path==="/conversations/search"){const body=JSON.parse(String(init?.body));expect(body.title_query).toBe("PRIVATE_TITLE");searches++;return Promise.resolve(json({items:[{id:"found"+searches,mode:"rag",title:"match",revision:0,epoch:0,updated_at:"2031-01-01T00:00:00Z"}],next_cursor:searches===1?"cursor":null}));}
      return Promise.resolve(json({items:[],next_cursor:null}));
    });vi.stubGlobal("fetch",fetch);render(<HistoryDirectory mode="rag" activeId="" revision={0} onOpen={vi.fn()}/>);
    await user.type(screen.getByRole("textbox",{name:"搜索历史标题"}),"PRIVATE_TITLE");await user.click(screen.getByRole("button",{name:"搜索"}));await screen.findByText("match");await user.click(screen.getByRole("button",{name:"加载更多对话"}));await waitFor(()=>expect(searches).toBe(2));expect(fetch.mock.calls.every(([path])=>!path.includes("PRIVATE_TITLE"))).toBe(true);
  });
  it("counts actionable requests and focuses the actual card without unlocking unknown submissions",async()=>{
    let state=conversationReducer(initialConversationState,{type:"start",question:"question"});
    state=conversationReducer(state,{type:"sync_interactions",interactions:[{kind:"approval",id:"one",toolName:"calculator",source:"native",provider:null,category:"utility",params:{},status:"pending",error:""},{kind:"url",id:"two",serverId:"test",url:"https://example.org",elicitationId:null,message:"",status:"outcome_unknown",error:""}]});
    render(<ConversationView mode="agent" state={state} onInteractionDecision={vi.fn()}/>);
    const target=document.getElementById("interaction-approval-one");expect(target).not.toBeNull();Object.defineProperty(target,"scrollIntoView",{value:vi.fn()});await userEvent.click(screen.getByRole("button",{name:"待处理 1 项，前往处理"}));expect(target).toHaveFocus();expect(screen.getByRole("button",{name:"已完成，继续"})).toBeDisabled();
  });
  it("ignores composition and AltGraph shortcuts and offers input/history focus",async()=>{
    window.history.replaceState({},"","/");vi.stubGlobal("fetch",vi.fn((path:string)=>Promise.resolve(json(path==="/workspace/models"?{items:MODELS.items,default_model:"flash"}:path==="/ready"?{status:"ready"}:{items:[],next_cursor:null}))));
    render(<App account={{id:"alice",username:"alice",role:"member",must_change_password:false}}/>);
    const input=screen.getByRole("textbox",{name:"向知识库提问"});
    const altGraph=new KeyboardEvent("keydown",{key:"h",ctrlKey:true,altKey:true});Object.defineProperty(altGraph,"getModifierState",{value:(name:string)=>name==="AltGraph"});window.dispatchEvent(altGraph);expect(screen.getByRole("textbox",{name:"搜索历史标题"})).not.toHaveFocus();
    fireEvent.keyDown(window,{key:"h",ctrlKey:true,altKey:true,isComposing:true});expect(screen.getByRole("textbox",{name:"搜索历史标题"})).not.toHaveFocus();
    fireEvent.keyDown(window,{key:"i",ctrlKey:true,altKey:true});await waitFor(()=>expect(input).toHaveFocus());fireEvent.keyDown(window,{key:"h",ctrlKey:true,altKey:true});await waitFor(()=>expect(screen.getByRole("textbox",{name:"搜索历史标题"})).toHaveFocus());
  });
  it.each(["ctrlKey", "metaKey"] as const)("does not interrupt native creator conversion with %s shortcuts", async modifier => {
    const originalShow = Object.getOwnPropertyDescriptor(HTMLDialogElement.prototype, "showModal");
    Object.defineProperty(HTMLDialogElement.prototype, "showModal", {
      configurable: true, value(this: HTMLDialogElement): void { this.open = true; },
    });
    const running: {signal: AbortSignal | null} = {signal: null};
    const cancelled = vi.fn();
    const fetch = vi.fn((path: string, init?: RequestInit): Promise<Response> => {
      if (path === "/workspace/models") return Promise.resolve(json({items: MODELS.items, default_model: "flash"}));
      if (path === "/ready") return Promise.resolve(json({status: "ready"}));
      if (path === "/skills") return Promise.resolve(json({items: [{id: "skill-creator", name: "skill-creator", description: "synthetic", builtin: true, enabled: true, revision: 1, version: "synthetic"}]}));
      if (path === "/skills/creator/stream") {
        running.signal = init?.signal ?? null;
        return Promise.resolve(new Response(new ReadableStream<Uint8Array>({
          start(controller): void { controller.enqueue(new TextEncoder().encode('data: {"chunk":"synthetic preview"}\n\n')); },
          cancel: cancelled,
        }), {headers: {"Content-Type": "text/event-stream"}}));
      }
      return Promise.resolve(json({items: [], next_cursor: null}));
    });
    vi.stubGlobal("fetch", fetch);
    window.history.replaceState({}, "", "/settings/skills");
    const user = userEvent.setup();
    const app = render(<App account={{id: "alice", username: "alice", role: "member", must_change_password: false}}/>);
    try {
      await user.click(await screen.findByRole("button", {name: "从资料生成技能"}));
      const dialog = screen.getByRole("dialog", {name: "从资料生成技能"});
      expect(dialog).toHaveAttribute("open");
      expect(dialog).not.toHaveAttribute("role");
      await user.upload(within(dialog).getByLabelText("导入原始资料"), new File(["synthetic"], "notes.txt", {type: "text/plain"}));
      await user.click(within(dialog).getByRole("button", {name: "开始整理"}));
      await waitFor(() => expect(within(dialog).getByLabelText("技能转换预览")).toHaveTextContent("synthetic preview"));
      const focus = within(dialog).getByRole("button", {name: "停止转换"});
      focus.focus();
      for (const key of ["n", "h", "i"]) {
        fireEvent.keyDown(focus, {key, altKey: true, [modifier]: true});
        expect(window.location.pathname).toBe("/settings/skills");
        expect(focus).toHaveFocus();
        expect(dialog).toBeInTheDocument();
        expect(within(dialog).getByLabelText("技能转换预览")).toHaveTextContent("synthetic preview");
        expect(running.signal).not.toBeNull();
        expect(running.signal?.aborted).toBe(false);
        expect(cancelled).not.toHaveBeenCalled();
        expect(fetch.mock.calls.filter(([path, init]) => path === "/conversations" && init?.method === "POST")).toHaveLength(0);
      }
    } finally {
      app.unmount();
      if (originalShow) Object.defineProperty(HTMLDialogElement.prototype, "showModal", originalShow);
      else Reflect.deleteProperty(HTMLDialogElement.prototype, "showModal");
    }
  });
  it("keeps a tool example draft after an existing Agent history finishes loading",async()=>{
    const user=userEvent.setup();window.history.replaceState({},"","/agent?conversation=saved");let delayed=false;let release:((value:Response)=>void)|undefined;
    const saved={id:"saved",mode:"agent",title:"saved conversation",model:"flash",epoch:0,revision:0,updated_at:"2031-01-01T00:00:00Z"};
    vi.stubGlobal("fetch",vi.fn((path:string):Promise<Response>=>{
      if(path==="/workspace/models")return Promise.resolve(json({items:MODELS.items,default_model:"flash"}));
      if(path==="/ready")return Promise.resolve(json({status:"ready"}));
      if(path==="/agent/tools")return Promise.resolve(json({tools:[{name:"calculator",description:"math",source:"native",provider:null,category:"utility",available:true}],mcp_servers:[]}));
      if(path==="/conversations/saved/turns")return delayed?new Promise(resolve=>{release=resolve;}):Promise.resolve(json({items:[],next_before:null}));
      if(path==="/conversations/saved")return Promise.resolve(json(saved));
      return Promise.resolve(json({items:[],next_cursor:null}));
    }));
    render(<App account={{id:"alice",username:"alice",role:"member",must_change_password:false}}/>);await waitFor(()=>expect(screen.getByRole("combobox",{name:"模型"})).toBeEnabled());
    await user.click(screen.getByRole("link",{name:"工具与 MCP"}));await screen.findByText("calculator");delayed=true;await user.click(screen.getByRole("button",{name:"将计算示例填入 Agent"}));await waitFor(()=>expect(release).toBeDefined());
    await act(async()=>release?.(json({items:[],next_before:null})));await waitFor(()=>expect(screen.getByRole("textbox",{name:"向 Agent 发送任务"})).toHaveValue("请使用计算器计算 (12 + 21) * 12，给出结果。"));
  });
  it("keyboard new from a utility page never reopens the old conversation or cancels its delayed creation",async()=>{
    const user=userEvent.setup();window.history.replaceState({},"","/agent?conversation=saved");let createdSignal:AbortSignal|undefined;let release:((value:Response)=>void)|undefined;
    const saved={id:"saved",mode:"agent",title:"saved conversation",model:"flash",epoch:0,revision:0,updated_at:"2031-01-01T00:00:00Z"};let nextId="";
    const fetch=vi.fn((path:string,init?:RequestInit):Promise<Response>=>{
      if(path==="/workspace/models")return Promise.resolve(json({items:MODELS.items,default_model:"flash"}));
      if(path==="/ready")return Promise.resolve(json({status:"ready"}));
      if(path==="/agent/tools")return Promise.resolve(json({tools:[],mcp_servers:[]}));
      if(path==="/conversations"&&init?.method==="POST"){nextId=JSON.parse(String(init.body)).id;createdSignal=init.signal??undefined;return new Promise(resolve=>{release=resolve;});}
      if(path==="/conversations/saved/turns")return Promise.resolve(json({items:[],next_before:null}));
      if(path==="/conversations/saved")return Promise.resolve(json(saved));
      return Promise.resolve(json({items:[],next_cursor:null}));
    });vi.stubGlobal("fetch",fetch);
    render(<App account={{id:"alice",username:"alice",role:"member",must_change_password:false}}/>);await waitFor(()=>expect(screen.getByRole("combobox",{name:"模型"})).toBeEnabled());await user.click(screen.getByRole("link",{name:"工具与 MCP"}));await screen.findByText("当前账号没有可见工具。");
    const oldReads=fetch.mock.calls.filter(([path])=>path==="/conversations/saved").length;fireEvent.keyDown(window,{key:"n",ctrlKey:true,altKey:true});await waitFor(()=>expect(release).toBeDefined());
    expect(createdSignal?.aborted).toBe(false);expect(fetch.mock.calls.filter(([path])=>path==="/conversations/saved")).toHaveLength(oldReads);
    await act(async()=>release?.(json({...saved,id:nextId,title:""})));await waitFor(()=>expect(new URLSearchParams(location.search).get("conversation")).toBe(nextId));
  });
});
