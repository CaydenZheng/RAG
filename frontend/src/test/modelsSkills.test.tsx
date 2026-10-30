import { act, fireEvent, render, renderHook, screen, waitFor, within } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { createHash, webcrypto } from "node:crypto";
import userEvent from "@testing-library/user-event";

import { createSkill, readModels, type ModelOption } from "../api/workspaceClient";
import { AUTH_EXPIRED_EVENT } from "../api/authClient";
import { readHistory } from "../api/conversationClient";
import { useConversation } from "../app/useConversation";
import { SkillsPage } from "../components/SkillsPage";
import { Composer } from "../components/Composer";
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
    render(<ConversationView mode="agent" state={{status:"idle",turns:page.items,nextTurnId:2}} onInteractionDecision={vi.fn()} />);
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
  it("uploads a skill and reports the preserved enabled state",async()=>{
    const custom = {...CREATOR,id:"custom",name:"concise",builtin:false,enabled:true};
    const fetch = vi.fn((path:string,init?:RequestInit):Promise<Response>=>{
      if(init?.method === "POST") return Promise.resolve(json(custom));
      return Promise.resolve(json({items:[{...CREATOR,enabled:true},custom]}));
    });
    vi.stubGlobal("fetch",fetch);
    const user=userEvent.setup();
    render(<SkillsPage available />);
    const toggle=await screen.findByRole("checkbox",{name:"启用 concise"});
    await waitFor(()=>expect(toggle).toBeEnabled());
    await user.upload(screen.getByLabelText("上传技能"),new File([CONTENT],"concise.md",{type:"text/markdown"}));
    await waitFor(()=>expect(screen.getByRole("status")).toHaveTextContent("当前启用"));
    expect(screen.getByRole("status")).not.toHaveTextContent("当前关闭");
    expect(toggle).toBeChecked();
    expect(fetch.mock.calls.some(([,init])=>init?.method === "PATCH")).toBe(false);
  });

  it("opens details from the row menu and returns focus to its trigger",async()=>{
    vi.stubGlobal("fetch",vi.fn((path:string):Promise<Response>=>Promise.resolve(json(path==="/skills"?{items:[CREATOR]}:{...CREATOR,content:"Safe instructions"}))));
    render(<SkillsPage available />);
    const trigger=await screen.findByRole("button",{name:"更多操作 skill-creator"});
    await waitFor(()=>expect(trigger).toBeEnabled());
    fireEvent.click(trigger);
    fireEvent.click(await screen.findByRole("menuitem",{name:"详情"}));
    const close=await screen.findByRole("button",{name:"关闭详情"});
    await waitFor(()=>expect(close).toBeEnabled());
    expect(screen.getByRole("region",{name:"技能详情"})).toBeVisible();
    expect(screen.getByText("Safe instructions")).toBeVisible();
    close.focus();
    fireEvent.click(close);
    expect(trigger).toHaveFocus();
    expect(screen.queryByRole("region",{name:"技能详情"})).not.toBeInTheDocument();
  });

  it("deletes a custom skill from the row menu but keeps builtins",async()=>{
    const custom = {...CREATOR,id:"custom",name:"concise",builtin:false};
    const fetch = vi.fn((path:string,init?:RequestInit):Promise<Response>=>{
      if(init?.method === "DELETE") return Promise.resolve(json({}));
      return Promise.resolve(json({items:[CREATOR,custom]}));
    });
    vi.stubGlobal("fetch",fetch);
    vi.spyOn(window,"confirm").mockReturnValue(true);
    render(<SkillsPage available />);
    const builtin = await screen.findByRole("button",{name:"更多操作 skill-creator"});
    await waitFor(()=>expect(builtin).toBeEnabled());
    fireEvent.click(builtin);
    expect(screen.queryByRole("menuitem",{name:"删除"})).not.toBeInTheDocument();
    fireEvent.keyDown(document,{key:"Escape"});

    fireEvent.click(screen.getByRole("button",{name:"更多操作 concise"}));
    fireEvent.click(await screen.findByRole("menuitem",{name:"删除"}));
    await waitFor(()=>expect(fetch.mock.calls.some(([,init])=>init?.method === "DELETE")).toBe(true));
  });
});

describe("agent skill selection transport",()=>{
  it("sends the selected skill id with the stripped message",async()=>{
    const user=userEvent.setup();
    window.history.replaceState({},"","/agent?conversation=saved");
    const skills=[{...CREATOR,id:"c1",name:"concise",description:"Be concise.",builtin:false,enabled:true}];
    const fetch=vi.fn((path:string,init?:RequestInit):Promise<Response>=>{
      if(path==="/ready")return Promise.resolve(json({status:"ready"}));
      if(path==="/workspace/models")return Promise.resolve(json({items:MODELS,default_model:"flash"}));
      if(path==="/skills")return Promise.resolve(json({items:skills}));
      if(path.startsWith("/conversations?"))return Promise.resolve(json({items:[{...SUMMARY,mode:"agent"}],next_cursor:null}));
      if(path.endsWith("/turns"))return Promise.resolve(json({items:[],next_before:null}));
      if(path==="/agent/chat/stream")return Promise.resolve(sse({done:true,answer:"ok",error:""}));
      if(path.startsWith("/agent/approvals/")||path.startsWith("/agent/elicitation/"))return Promise.resolve(json({pending:[]}));
      if(init?.method==="PATCH")return Promise.resolve(json({...SUMMARY,mode:"agent"}));
      return Promise.resolve(json({...SUMMARY,mode:"agent"}));
    });
    vi.stubGlobal("fetch",fetch);
    render(<App account={{id:"owner",username:"alice",role:"member",must_change_password:false}} />);
    const input=await screen.findByRole("textbox",{name:"向 Agent 发送任务"});
    await user.type(input,"/");
    await screen.findByRole("listbox",{name:"选择技能"});
    await user.keyboard("{Enter}");
    await user.type(input,"整理要点");
    await user.click(screen.getByRole("button",{name:"发送"}));
    await waitFor(()=>expect(fetch.mock.calls.some(([path])=>path==="/agent/chat/stream")).toBe(true));
    const call=fetch.mock.calls.find(([path])=>path==="/agent/chat/stream");
    expect(JSON.parse(String(call?.[1]?.body))).toMatchObject({message:"整理要点",skill_ids:["c1"]});
  });
});

describe("composer skill picker",()=>{
  const CONCISE = {...CREATOR,id:"c1",name:"concise",description:"Be concise.",builtin:false,enabled:true};
  const skills=[CONCISE,{...CREATOR,id:"off",name:"disabled-skill",builtin:false,enabled:false}];

  function Harness({onSend}:{onSend:(message:string,skillIds?:readonly string[])=>Promise<void>}): React.JSX.Element {
    const [value,setValue]=useState("");
    return <Composer mode="agent" status="idle" resetting={false} skillsEnabled value={value} onValueChange={setValue} onSend={onSend} onStop={vi.fn()} />;
  }

  it("lists only enabled custom skills and inserts the chosen name",async()=>{
    vi.stubGlobal("fetch",vi.fn().mockResolvedValue(json({items:skills})));
    const user=userEvent.setup();
    render(<Harness onSend={vi.fn()} />);
    const input=screen.getByRole("textbox",{name:"向 Agent 发送任务"});
    await user.type(input,"/");
    const list=await screen.findByRole("listbox",{name:"选择技能"});
    expect(within(list).getByText("concise")).toBeVisible();
    expect(within(list).queryByText("disabled-skill")).not.toBeInTheDocument();
    expect(within(list).queryByText("skill-creator")).not.toBeInTheDocument();
    await user.keyboard("{Enter}");
    expect(input).toHaveValue("/concise ");
    expect(screen.queryByRole("listbox",{name:"选择技能"})).not.toBeInTheDocument();
  });

  it("sends the stripped body with the selected skill id",async()=>{
    vi.stubGlobal("fetch",vi.fn().mockResolvedValue(json({items:skills})));
    const onSend=vi.fn().mockResolvedValue(undefined);
    const user=userEvent.setup();
    render(<Harness onSend={onSend} />);
    const input=screen.getByRole("textbox",{name:"向 Agent 发送任务"});
    await user.type(input,"/");
    await screen.findByRole("listbox",{name:"选择技能"});
    await user.keyboard("{Enter}");
    await user.type(input,"整理部署要点");
    await user.click(screen.getByRole("button",{name:"发送"}));
    await waitFor(()=>expect(onSend).toHaveBeenCalledWith("整理部署要点",["c1"]));
  });

  it("blocks sending a bare command and unknown skill names",async()=>{
    vi.stubGlobal("fetch",vi.fn().mockResolvedValue(json({items:skills})));
    const onSend=vi.fn().mockResolvedValue(undefined);
    const user=userEvent.setup();
    render(<Harness onSend={onSend} />);
    const input=screen.getByRole("textbox",{name:"向 Agent 发送任务"});
    await user.type(input,"/concise");
    await waitFor(()=>expect(screen.getByRole("button",{name:"请补充任务内容后再发送"})).toBeDisabled());
    fireEvent.submit(input.closest("form")!);
    expect(onSend).not.toHaveBeenCalled();

    await user.clear(input);
    await user.type(input,"/nope 做点什么");
    await waitFor(()=>expect(screen.getByRole("button",{name:"所选技能不可用，请刷新技能列表后重试"})).toBeDisabled());
    fireEvent.submit(input.closest("form")!);
    expect(onSend).not.toHaveBeenCalled();
  });

  it("sends plain text without skill ids when no command is present",async()=>{
    vi.stubGlobal("fetch",vi.fn().mockResolvedValue(json({items:skills})));
    const onSend=vi.fn().mockResolvedValue(undefined);
    const user=userEvent.setup();
    render(<Harness onSend={onSend} />);
    await user.type(screen.getByRole("textbox",{name:"向 Agent 发送任务"}),"普通任务");
    await user.click(screen.getByRole("button",{name:"发送"}));
    await waitFor(()=>expect(onSend).toHaveBeenCalledWith("普通任务"));
  });
});
