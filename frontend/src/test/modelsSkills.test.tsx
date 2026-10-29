import { act, fireEvent, render, renderHook, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { createHash, webcrypto } from "node:crypto";
import userEvent from "@testing-library/user-event";

import { createSkill, readModels, type ModelOption } from "../api/workspaceClient";
import { AUTH_EXPIRED_EVENT } from "../api/authClient";
import { readHistory } from "../api/conversationClient";
import { useConversation } from "../app/useConversation";
import { SkillsPage } from "../components/SkillsPage";
import { ConversationView } from "../components/ConversationView";
import { App } from "../app/App";

const MODELS: ModelOption[] = [{id:"flash",label:"DeepSeek V4-Flash",available:true},{id:"pro",label:"DeepSeek V4-Pro",available:true}];
const SUMMARY = {id:"saved",mode:"rag",title:"question",model:"flash",revision:1,epoch:0,updated_at:"2026-10-27T00:00:00Z"};
const CREATOR = {id:"skill-creator",name:"skill-creator",description:"Convert documents",builtin:true,enabled:false,revision:0,version:"a".repeat(64)};
const CONTENT = "---\nschema_version: 1\nname: concise\ndescription: Concise writing\n---\nBe concise.\n";
function json(value: unknown, status = 200): Response { return new Response(JSON.stringify(value),{status,headers:{"Content-Type":"application/json"}}); }
function sse(...values: unknown[]): Response {return new Response(values.map(value=>`data: ${JSON.stringify(value)}\n\n`).join(""),{headers:{"Content-Type":"text/event-stream"}});}

describe("model selection lifecycle", () => {
  it.each(["rag", "agent"] as const)("lets an idle %s conversation recover from unavailable Pro through confirmed model PATCH", async (mode) => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", `${mode === "rag" ? "/" : "/agent"}?conversation=saved`);
    let current = {...SUMMARY, mode, model:"pro"};
    let finish: ((response: Response) => void) | undefined;
    const fetch = vi.fn((path: string, init?: RequestInit): Promise<Response> => {
      if (path === "/ready") return Promise.resolve(json({status:"ready"}));
      if (path === "/workspace/models") return Promise.resolve(json({items:MODELS.map(item=>({...item,available:item.id === "flash"})),default_model:"flash"}));
      if (init?.method === "PATCH") return new Promise(resolve=>{finish=resolve;});
      if (path.startsWith("/conversations?")) return Promise.resolve(json({items:[current],next_cursor:null}));
      if (path.endsWith("/turns")) return Promise.resolve(json({items:[],next_before:null}));
      if (path.startsWith("/query/stream")) return Promise.resolve(sse({chunk:"answer"},{done:true}));
      if (path === "/agent/chat/stream") return Promise.resolve(sse({done:true,answer:"answer",error:""}));
      if (path.startsWith("/agent/approvals/") || path.startsWith("/agent/elicitation/")) return Promise.resolve(json({pending:[]}));
      return Promise.resolve(json(current));
    });
    vi.stubGlobal("fetch", fetch);
    render(<App account={{id:"owner",username:"alice",role:"member",must_change_password:false}} />);
    const selector = await screen.findByRole("combobox",{name:"模型"});
    await waitFor(()=>expect(selector).toHaveValue("pro"));
    expect(selector).toBeEnabled();
    expect(screen.getByText("当前模型未配置，请选择可用模型",{selector:"p"})).toBeVisible();
    expect(screen.getByRole("button",{name:"当前模型未配置，请选择可用模型"})).toBeDisabled();
    await user.selectOptions(selector,"flash");
    await waitFor(()=>expect(finish).toBeDefined());
    expect(selector).toHaveValue("pro");
    expect(selector).toBeDisabled();
    current={...current,model:"flash",revision:2};
    await act(async()=>{finish?.(json(current));});
    await waitFor(()=>expect(selector).toHaveValue("flash"));
    expect(selector).toBeEnabled();
    await user.type(screen.getByRole("textbox",{name:mode === "rag" ? "向知识库提问" : "向 Agent 发送任务"}),"question");
    await user.click(screen.getByRole("button",{name:"发送"}));
    await waitFor(()=>expect(fetch.mock.calls.some(([path])=>path.startsWith("/query/stream") || path === "/agent/chat/stream")).toBe(true));
    const request = fetch.mock.calls.find(([path])=>path.startsWith("/query/stream") || path === "/agent/chat/stream");
    expect(mode === "rag" ? new URL(request?.[0]??"","https://test").searchParams.get("model") : JSON.parse(String(request?.[1]?.body)).model).toBe("flash");
  });
  it("rejects an unknown catalog default instead of guessing Flash", async () => {
    vi.stubGlobal("fetch",vi.fn().mockResolvedValue(json({items:MODELS,default_model:"unknown"})));
    await expect(readModels(new AbortController().signal)).rejects.toThrow("模型目录暂不可用");
  });

  it("waits for model PATCH, blocks sends, and reconciles a lost response without replay", async () => {
    let finish: ((response: Response)=>void) | undefined;
    let current = {...SUMMARY};
    const fetch = vi.fn((path: string,init?: RequestInit): Promise<Response>=>{
      if(init?.method==="PATCH")return new Promise(resolve=>{finish=resolve;});
      return Promise.resolve(json(path.endsWith("/turns")?{items:[],next_before:null}:current));
    });
    vi.stubGlobal("fetch",fetch);
    const hook=renderHook(()=>useConversation("rag",true));
    await act(()=>hook.result.current.open("saved"));
    let changing: Promise<void> | undefined;
    act(()=>{changing=hook.result.current.chooseModel("pro");});
    expect(hook.result.current.model).toBe("flash");
    await act(()=>hook.result.current.send("blocked"));
    expect(fetch.mock.calls.some(([path])=>path.startsWith("/query"))).toBe(false);
    current={...SUMMARY,model:"pro",revision:2};
    await act(async()=>{finish?.(json({},503));await changing;});
    expect(hook.result.current.model).toBe("pro");
    expect(hook.result.current.modelUnconfirmed).toBe(false);
    expect(fetch.mock.calls.filter(([,init])=>init?.method==="PATCH")).toHaveLength(1);
  });

  it("locks an unconfirmed model change until the conversation is reopened", async () => {
    let unavailable=true;
    vi.stubGlobal("fetch",vi.fn((path:string,init?:RequestInit):Promise<Response>=>{
      if(init?.method==="PATCH" || unavailable&&path==="/conversations/saved")return Promise.reject(new TypeError("PRIVATE"));
      return Promise.resolve(json(path.endsWith("/turns")?{items:[]}:SUMMARY));
    }));
    const hook=renderHook(()=>useConversation("rag",true));
    unavailable=false;await act(()=>hook.result.current.open("saved"));unavailable=true;
    await act(()=>hook.result.current.chooseModel("pro"));
    expect(hook.result.current.model).toBe("flash");
    expect(hook.result.current.modelUnconfirmed).toBe(true);
    await act(()=>hook.result.current.send("blocked"));
    expect(hook.result.current.state.turns).toHaveLength(0);
    expect(hook.result.current.notice).not.toContain("PRIVATE");
    unavailable=false;await act(()=>hook.result.current.open("saved"));
    expect(hook.result.current.modelUnconfirmed).toBe(false);
  });

  it("fixes the model before conversation creation and refuses changes during the stream", async () => {
    let close: ReadableStreamDefaultController<Uint8Array> | undefined;
    const fetch=vi.fn((path:string,init?:RequestInit):Promise<Response>=>{
      if(path.startsWith("/query"))return Promise.resolve(new Response(new ReadableStream<Uint8Array>({start(controller){close=controller;}}),{headers:{"Content-Type":"text/event-stream"}}));
      return Promise.resolve(json({...SUMMARY,id:init?.body?JSON.parse(String(init.body)).id:"saved",model:"pro"}));
    });
    vi.stubGlobal("fetch",fetch);
    const hook=renderHook(()=>useConversation("rag",true));
    await act(()=>hook.result.current.chooseModel("pro"));
    let sending:Promise<void>|undefined;act(()=>{sending=hook.result.current.send("question");});
    await waitFor(()=>expect(close).toBeDefined());
    await act(()=>hook.result.current.chooseModel("flash"));
    expect(hook.result.current.model).toBe("pro");
    const request=fetch.mock.calls.find(([path])=>path.startsWith("/query"));
    expect(new URL(request?.[0]??"","https://test").searchParams.get("model")).toBe("pro");
    await act(async()=>{hook.result.current.stop();await sending;});
    expect(request?.[1]?.signal?.aborted).toBe(true);
  });

  it("renders answer-time model and skill version from history", async () => {
    vi.stubGlobal("fetch",vi.fn().mockResolvedValue(json({items:[{seq:1,client_turn_id:"turn",question:"q",answer:"a",used_model:"historical-model",requested_model:"pro",skills:[{id:"s",name:"concise",version:"0123456789abcdef"}]}]})));
    const page=await readHistory("saved",new AbortController().signal);
    render(<ConversationView mode="agent" state={{status:"idle",turns:page.items,nextTurnId:2}} onSuggestion={vi.fn()} onInteractionDecision={vi.fn()} />);
    expect(screen.getByText("实际模型 historical-model")).toBeVisible();
    expect(screen.getByText("技能 concise · 01234567")).toBeVisible();
  });
});

describe("creator transport",()=>{
  it("expires the account boundary on a revoked creator stream",async()=>{
    const expired=vi.fn();window.addEventListener(AUTH_EXPIRED_EVENT,expired);
    vi.stubGlobal("fetch",vi.fn().mockResolvedValue(sse({code:"auth_required",error:"fixed public message"})));
    try{
      await expect(createSkill(new File(["x"],"x.txt"),"flash","op",new AbortController().signal,vi.fn())).rejects.toThrow();
      expect(expired).toHaveBeenCalledOnce();
    }finally{window.removeEventListener(AUTH_EXPIRED_EVENT,expired);}
  });
  it("accepts only a complete checksum-verified artifact and cancels its reader",async()=>{
    vi.stubGlobal("crypto",webcrypto);
    const checksum=createHash("sha256").update(CONTENT).digest("hex");
    const fetch=vi.fn().mockResolvedValue(sse({chunk:CONTENT},{done:true,filename:"concise.md",content:CONTENT,checksum}));
    vi.stubGlobal("fetch",fetch);
    const update=vi.fn();
    await expect(createSkill(new File(["notes"],"notes.txt"),"pro","operation",new AbortController().signal,update)).resolves.toMatchObject({content:CONTENT});
    expect(update).toHaveBeenCalledWith(CONTENT);
    const data:FormData=fetch.mock.calls[0][1].body;
    expect(data.get("model")).toBe("pro");
    fetch.mockResolvedValue(sse({done:true,filename:"concise.md",content:CONTENT,checksum:"0".repeat(64)}));
    await expect(createSkill(new File(["x"],"x.txt"),"flash","op2",new AbortController().signal,vi.fn())).rejects.toThrow("校验失败");
  });

  it.each([{events:[]},{events:[{chunk:"preview"}]},{events:[{error:{code:"creator_failed",message:"PRIVATE"},done:true}]}])("does not install or mark a missing/error terminal successful: %j",async({events})=>{
    const fetch=vi.fn().mockResolvedValue(sse(...events));vi.stubGlobal("fetch",fetch);
    await expect(createSkill(new File(["x"],"x.txt"),"flash","op",new AbortController().signal,vi.fn())).rejects.not.toThrow("PRIVATE");
    expect(fetch).toHaveBeenCalledTimes(1);
  });
});

describe("Skills page",()=>{
  it.each(["plain", "creator"])("preserves and reports an enabled skill on idempotent %s upload",async(origin)=>{
    vi.stubGlobal("crypto",webcrypto);
    const checksum = createHash("sha256").update(CONTENT).digest("hex");
    const custom = {...CREATOR,id:"custom",name:"concise",builtin:false,enabled:true};
    const fetch = vi.fn((path:string,init?:RequestInit):Promise<Response>=>{
      if(path === "/skills/creator/stream") return Promise.resolve(sse({done:true,content:CONTENT,filename:"concise.md",checksum}));
      if(init?.method === "POST") return Promise.resolve(json(custom));
      return Promise.resolve(json({items:[{...CREATOR,enabled:true},custom]}));
    });
    vi.stubGlobal("fetch",fetch);
    const user=userEvent.setup();
    render(<SkillsPage available models={MODELS} defaultModel="flash" />);
    const toggle=await screen.findByRole("checkbox",{name:"启用 concise"});
    await waitFor(()=>expect(toggle).toBeEnabled());
    if(origin === "plain") {
      await user.upload(screen.getByLabelText("选择技能文件"),new File([CONTENT],"concise.md",{type:"text/markdown"}));
      await user.click(screen.getByRole("button",{name:"上传技能"}));
    } else {
      await user.upload(screen.getByLabelText("导入原始资料"),new File(["notes"],"notes.txt",{type:"text/plain"}));
      await user.click(screen.getByRole("button",{name:"开始整理"}));
      await user.click(await screen.findByRole("button",{name:/上传此技能/}));
    }
    await waitFor(()=>expect(screen.getByRole("status")).toHaveTextContent("当前启用"));
    expect(screen.getByRole("status")).not.toHaveTextContent("当前关闭");
    expect(toggle).toBeChecked();
    expect(fetch.mock.calls.some(([,init])=>init?.method === "PATCH")).toBe(false);
  });
  it("returns keyboard focus to the detail trigger after closing inline details",async()=>{
    vi.stubGlobal("fetch",vi.fn((path:string):Promise<Response>=>Promise.resolve(json(path==="/skills"?{items:[CREATOR]}:{...CREATOR,content:"Safe instructions"}))));
    render(<SkillsPage available models={MODELS} defaultModel="flash" />);
    const trigger=await screen.findByRole("button",{name:"查看 skill-creator"});
    await waitFor(()=>expect(trigger).toBeEnabled());fireEvent.click(trigger);
    const close=await screen.findByRole("button",{name:"关闭详情"});
    await waitFor(()=>expect(close).toBeEnabled());close.focus();fireEvent.click(close);
    expect(trigger).toHaveFocus();
    expect(screen.queryByRole("region",{name:"技能详情"})).not.toBeInTheDocument();
  });
  it("enables creator explicitly; completed output requires a separate upload and stays disabled",async()=>{
    vi.stubGlobal("crypto",webcrypto);
    const checksum=createHash("sha256").update(CONTENT).digest("hex");
    let creator={...CREATOR};const uploaded:unknown[]=[];
    const fetch=vi.fn((path:string,init?:RequestInit):Promise<Response>=>{
      if(path==="/skills/creator/stream")return Promise.resolve(sse({chunk:CONTENT},{done:true,filename:"concise.md",content:CONTENT,checksum}));
      if(init?.method==="PATCH"){creator={...creator,enabled:true,revision:1};return Promise.resolve(json(creator));}
      if(init?.method==="POST"){uploaded.push(init.body);return Promise.resolve(json({...CREATOR,id:"custom",name:"concise",builtin:false}));}
      return Promise.resolve(json({items:[creator]}));
    });vi.stubGlobal("fetch",fetch);
    render(<SkillsPage available models={MODELS} defaultModel="flash" />);
    const toggle=await screen.findByRole("checkbox",{name:"启用 skill-creator"});
    await waitFor(()=>expect(toggle).toBeEnabled());
    expect(screen.getByLabelText("导入原始资料")).toBeDisabled();
    expect(screen.queryByRole("button",{name:"删除 skill-creator"})).not.toBeInTheDocument();
    fireEvent.click(toggle);
    await waitFor(()=>expect(screen.getByLabelText("导入原始资料")).toBeEnabled());
    fireEvent.change(screen.getByLabelText("导入原始资料"),{target:{files:[new File(["notes"],"notes.txt")]}});
    fireEvent.click(screen.getByRole("button",{name:"开始整理"}));
    const install=await screen.findByRole("button",{name:"上传此技能（新项默认关闭）"});
    expect(uploaded).toHaveLength(0);
    fireEvent.click(install);
    await screen.findByText("技能已上传，当前关闭；请明确启用后使用。");
    expect(uploaded).toHaveLength(1);
  });

  it("unmount aborts an active conversion and discards late artifacts",async()=>{
    let signal:AbortSignal|undefined;
    vi.stubGlobal("fetch",vi.fn((path:string,init?:RequestInit):Promise<Response>=>{
      if(path==="/skills/creator/stream"){signal=init?.signal??undefined;return new Promise((_resolve,reject)=>signal?.addEventListener("abort",()=>reject(new DOMException("Aborted","AbortError"))));}
      return Promise.resolve(json({items:[{...CREATOR,enabled:true}]}));
    }));
    const page=render(<SkillsPage available models={MODELS} defaultModel="flash" />);
    await waitFor(()=>expect(screen.getByLabelText("导入原始资料")).toBeEnabled());
    fireEvent.change(screen.getByLabelText("导入原始资料"),{target:{files:[new File(["notes"],"notes.txt")]}});
    fireEvent.click(screen.getByRole("button",{name:"开始整理"}));
    await waitFor(()=>expect(signal).toBeDefined());page.unmount();
    expect(signal?.aborted).toBe(true);
  });
});
