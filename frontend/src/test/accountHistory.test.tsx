import { act, fireEvent, render, renderHook, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AccountBoundary } from "../app/AccountBoundary";
import { useConversation } from "../app/useConversation";
import { AUTH_EXPIRED_EVENT, clearAccount, readAccount, setAccountMode, workspaceFetch, type AccountUser } from "../api/authClient";
import { App } from "../app/App";
import { Composer } from "../components/Composer";
import { createConversation, readHistory } from "../api/conversationClient";
import { readSseEvents } from "../api/sse";

const SESSION = "dd0a4998-1e1f-4c1d-84dd-48e22bdfc055";
const SUMMARY = { id: SESSION,mode:"rag",title:"saved",revision:1,epoch:0,updated_at:"2026-10-22T00:00:00Z" };
const USER: AccountUser = { id:"owner",username:"alice",role:"member",must_change_password:false };
const json = (value: unknown, status = 200): Response => new Response(JSON.stringify(value),{status,headers:{"Content-Type":"application/json"}});

afterEach(() => { setAccountMode(false); clearAccount(); });

describe("account transport", () => {
  it("completes and reopens a saved Agent turn using the real successful DONE shape", async () => {
    const summary = { ...SUMMARY, mode: "agent" };
    let turnId = "";
    vi.stubGlobal("fetch", vi.fn((path: string, init?: RequestInit): Promise<Response> => {
      if (path === "/agent/chat/stream") {
        turnId = JSON.parse(String(init?.body)).client_turn_id;
        const done = { done: true, session_id: SESSION, answer: "complete", tool_calls: [],
          iterations: 0, latency_ms: 0, error_code: "", error: "" };
        return Promise.resolve(new Response(`data: {"chunk":"complete"}\n\ndata: ${JSON.stringify(done)}\n\n`,
          { headers: { "Content-Type": "text/event-stream" } }));
      }
      if (path.includes("/turns")) return Promise.resolve(json({ items: [{ seq: 1, question: "question",
        answer: "complete", client_turn_id: turnId, sources: [] }], next_before: null }));
      if (path.startsWith("/agent/approvals/") || path.startsWith("/agent/elicitation/")) {
        return Promise.resolve(json({ pending: [] }));
      }
      return Promise.resolve(json(summary));
    }));
    const hook = renderHook(() => useConversation("agent", true));
    await act(() => hook.result.current.send("question"));
    expect(hook.result.current.state.status).toBe("completed");
    expect(hook.result.current.unconfirmed).toBe(false);
    expect(turnId).not.toBe("");
    const saved = await readHistory(SESSION, new AbortController().signal, { clientTurnId: turnId });
    expect(saved.items[0].answer).toBe("complete");
    await act(() => hook.result.current.open(SESSION));
    expect(hook.result.current.state.status).toBe("idle");
    expect(hook.result.current.state.turns[0]).toMatchObject({
      status: "completed", answer: "complete", clientTurnId: turnId,
    });
  });

  it("does not dispatch a buffered old-account terminal after stream cancellation", async () => {
    const controller = new AbortController();
    const stream = new Response('data: {"chunk":"first"}\n\ndata: {"error":"expired","code":"auth_required"}\n\n').body;
    if (!stream) throw new Error("fixture body missing");
    const events = readSseEvents(stream,controller.signal);
    expect((await events.next()).value).toEqual({chunk:"first"});
    controller.abort();
    await expect(events.next()).rejects.toMatchObject({name:"AbortError"});
  });
  it("keeps CSRF only in memory and sends it even for side-effectful GET SSE", async () => {
    localStorage.clear();
    setAccountMode(true);
    const fetch = vi.fn().mockResolvedValueOnce(json({user:USER,csrf_token:"c".repeat(64)})).mockResolvedValue(json({}));
    vi.stubGlobal("fetch",fetch);
    await readAccount(new AbortController().signal);
    await workspaceFetch("/query/stream?query=synthetic");
    const init: RequestInit = fetch.mock.calls[1][1];
    expect(new Headers(init.headers).get("X-CSRF-Token")).toBe("c".repeat(64));
    expect(init.credentials).toBe("same-origin");
    expect(localStorage.length).toBe(0);
    clearAccount();
    await workspaceFetch("/ready");
    expect(new Headers(fetch.mock.calls[2][1].headers).has("X-CSRF-Token")).toBe(false);
  });

  it("emits expiry for private 401 and never forwards private error text", async () => {
    setAccountMode(true);
    const expired = vi.fn(); window.addEventListener(AUTH_EXPIRED_EVENT,expired);
    vi.stubGlobal("fetch",vi.fn().mockResolvedValue(json({detail:"private exception"},401)));
    try { await workspaceFetch("/conversations"); expect(expired).toHaveBeenCalledOnce(); }
    finally { window.removeEventListener(AUTH_EXPIRED_EVENT,expired); }
  });

  it("rejects corrupt or wrong-turn history instead of confirming another answer", async () => {
    vi.stubGlobal("fetch",vi.fn().mockResolvedValue(json({items:[{seq:1,question:"q",answer:"a",client_turn_id:"wrong"}]})));
    await expect(readHistory(SESSION,new AbortController().signal,{clientTurnId:SESSION})).rejects.toThrow("历史回合信息无效");
  });

  it("ignores a late cancelled login instead of replacing the next account CSRF", async () => {
    setAccountMode(true);
    let finish: ((response: Response) => void) | undefined;
    const fetch = vi.fn().mockImplementationOnce(() => new Promise<Response>((resolve) => { finish=resolve; }))
      .mockResolvedValueOnce(json({user:USER,csrf_token:"n".repeat(64)})).mockResolvedValue(json({}));
    vi.stubGlobal("fetch",fetch);
    const old = new AbortController();
    const pending = readAccount(old.signal);
    old.abort();
    await readAccount(new AbortController().signal);
    finish?.(json({user:{...USER,id:"old"},csrf_token:"o".repeat(64)}));
    await expect(pending).rejects.toMatchObject({name:"AbortError"});
    await workspaceFetch("/ready");
    expect(new Headers(fetch.mock.calls[2][1].headers).get("X-CSRF-Token")).toBe("n".repeat(64));
  });
});

describe("account login", () => {
  it("fails closed when configuration cannot be read", async () => {
    vi.stubGlobal("fetch",vi.fn().mockRejectedValue(new TypeError("private host")));
    render(<AccountBoundary />);
    expect(await screen.findByRole("heading",{name:"账号服务暂不可用"})).toBeVisible();
    expect(screen.queryByRole("textbox",{name:"消息"})).not.toBeInTheDocument();
    expect(screen.queryByText("private host")).not.toBeInTheDocument();
  });

  it("supports password managers and forces initial password change before workspace", async () => {
    vi.stubGlobal("fetch",vi.fn((path: string): Promise<Response> => Promise.resolve(path === "/workspace/config"
      ? json({auth_enabled:true}) : path === "/auth/me" ? json({},401)
        : json({user:{...USER,must_change_password:true},csrf_token:"c".repeat(64)}))));
    render(<AccountBoundary />);
    const username = await screen.findByLabelText("账号");
    expect(username).toHaveAttribute("autocomplete","username");
    fireEvent.change(username,{target:{value:"alice"}});
    const password = screen.getByLabelText("密码");
    expect(password).toHaveAttribute("autocomplete","current-password");
    fireEvent.change(password,{target:{value:"synthetic-password-123"}});
    fireEvent.click(screen.getByRole("button",{name:"登录"}));
    expect(await screen.findByRole("heading",{name:"修改初始密码"})).toBeVisible();
    expect(screen.getByLabelText("新密码")).toHaveAttribute("autocomplete","new-password");
    expect(localStorage.getItem("csrf_token")).toBeNull();
    expect(screen.queryByRole("heading",{name:"知识问答工作台"})).not.toBeInTheDocument();
  });
});

describe("account conversation lifecycle", () => {
  it("closes the title draft when switching conversation mode", async () => {
    window.history.replaceState({},"",`/?conversation=${SESSION}`);
    vi.stubGlobal("fetch",vi.fn((path: string): Promise<Response> => Promise.resolve(path === "/ready" ? json({status:"ready"})
      : path.startsWith("/conversations?") ? json({items:[SUMMARY],next_cursor:null})
        : path.endsWith("/turns") ? json({items:[],next_before:null}) : json(SUMMARY))));
    render(<App account={USER} />);
    const rename = await screen.findByRole("button",{name:"重命名"});
    await waitFor(() => expect(rename).toBeEnabled());
    fireEvent.click(rename);
    fireEvent.change(screen.getByLabelText("会话标题"),{target:{value:"private draft title"}});
    fireEvent.click(screen.getByRole("link",{name:"Agent"}));
    expect(screen.queryByLabelText("会话标题")).not.toBeInTheDocument();
    expect(screen.queryByDisplayValue("private draft title")).not.toBeInTheDocument();
  });

  it("unblocking the composer never steals focus from a conversation action", () => {
    const properties = {mode:"rag" as const,resetting:false,value:"",onValueChange:vi.fn(),onSend:vi.fn(),onStop:vi.fn()};
    const page = render(<><button type="button">会话操作</button><Composer {...properties} status="streaming" /></>);
    const action = screen.getByRole("button",{name:"会话操作"});
    action.focus();
    page.rerender(<><button type="button">会话操作</button><Composer {...properties} status="completed" /></>);
    expect(action).toHaveFocus();
  });

  it("preserves the scroll anchor when prepending an older transcript page", async () => {
    window.history.replaceState({},"",`/?conversation=${SESSION}`);
    let height = 1000;
    const turn = (seq: number): Record<string, unknown> => ({seq,question:`Question ${seq}`,answer:`Answer ${seq}`,client_turn_id:SESSION,sources:[]});
    vi.stubGlobal("fetch",vi.fn((path: string): Promise<Response> => {
      if (path === "/ready") return Promise.resolve(json({status:"ready"}));
      if (path.startsWith("/conversations?")) return Promise.resolve(json({items:[SUMMARY],next_cursor:null}));
      if (path.includes("/turns?before=")) { height=2000; return Promise.resolve(json({items:[turn(1)],next_before:null})); }
      if (path.endsWith("/turns")) return Promise.resolve(json({items:[turn(21)],next_before:"older"}));
      return Promise.resolve(json(SUMMARY));
    }));
    render(<App account={USER} />);
    expect(await screen.findByText("Answer 21",{exact:true})).toBeVisible();
    const region = screen.getByRole("region",{name:"对话内容"});
    Object.defineProperty(region,"scrollHeight",{configurable:true,get:() => height});
    Object.defineProperty(region,"clientHeight",{configurable:true,value:300});
    region.scrollTop=250; fireEvent.scroll(region);
    fireEvent.click(screen.getByRole("button",{name:"加载更早问答"}));
    expect(await screen.findByText("Answer 1",{exact:true})).toBeVisible();
    expect(region.scrollTop).toBe(1250);
  });

  it("new preserves the old page on failed creation and never calls reset", async () => {
    const fetch = vi.fn((path: string): Promise<Response> => path === "/conversations"
      ? Promise.reject(new TypeError("network")) : Promise.resolve(json(path.endsWith("/turns") ? {items:[],next_before:null} : SUMMARY)));
    vi.stubGlobal("fetch",fetch);
    const hook = renderHook(() => useConversation("rag",true));
    await act(() => hook.result.current.open(SESSION));
    expect(hook.result.current.sessionId).toBe(SESSION);
    await act(() => hook.result.current.reset());
    expect(hook.result.current.sessionId).toBe(SESSION);
    expect(hook.result.current.summary?.title).toBe("saved");
    expect(fetch.mock.calls.some(([path]) => path.includes("reset"))).toBe(false);
  });

  it("blocks sends during delayed new creation and rotates only on server success", async () => {
    let finish: ((response: Response) => void) | undefined;
    const fetch = vi.fn((path: string): Promise<Response> => path === "/conversations"
      ? new Promise((resolve) => { finish = resolve; }) : Promise.resolve(json({})));
    vi.stubGlobal("fetch",fetch);
    const hook = renderHook(() => useConversation("rag",true));
    let resetting: Promise<void> | undefined;
    act(() => { resetting = hook.result.current.reset(); });
    expect(hook.result.current.resetting).toBe(true);
    await act(() => hook.result.current.send("do not send"));
    expect(fetch).toHaveBeenCalledTimes(1);
    await act(async () => { finish?.(json(SUMMARY,201)); await resetting; });
    expect(hook.result.current.resetting).toBe(false);
    expect(hook.result.current.state.turns).toHaveLength(0);
  });

  it("an older cancelled new request cannot unlock a newer delayed creation", async () => {
    const resolvers: Array<(response: Response) => void> = [];
    const fetch = vi.fn(() => new Promise<Response>((resolve) => {resolvers.push(resolve);}));
    vi.stubGlobal("fetch",fetch);
    const hook = renderHook(() => useConversation("rag",true));
    let first: Promise<void> | undefined; let second: Promise<void> | undefined;
    act(() => {first=hook.result.current.reset();});
    act(() => hook.result.current.stop(true));
    act(() => {second=hook.result.current.reset();});
    await act(async () => {resolvers[0](json(SUMMARY,201));await first;});
    expect(hook.result.current.resetting).toBe(true);
    await act(() => hook.result.current.send("must remain blocked"));
    expect(fetch).toHaveBeenCalledTimes(2);
    await act(async () => {resolvers[1](json(SUMMARY,201));await second;});
    expect(hook.result.current.resetting).toBe(false);
  });

  it("rename uses the visible revision and does not silently overwrite a concurrent edit", async () => {
    const fetch = vi.fn((path: string,init?: RequestInit): Promise<Response> => Promise.resolve(init?.method === "PATCH"
      ? json({},409) : path.endsWith("/turns") ? json({items:[],next_before:null}) : json(SUMMARY)));
    vi.stubGlobal("fetch",fetch);
    const hook = renderHook(() => useConversation("rag",true));
    await act(() => hook.result.current.open(SESSION));
    let saved = true;
    await act(async () => { saved=await hook.result.current.mutate("rename","my edit"); });
    expect(saved).toBe(false);
    const patch = fetch.mock.calls.find(([,init]) => init?.method === "PATCH");
    expect(JSON.parse(String(patch?.[1]?.body))).toMatchObject({expected_revision:1});
    expect(hook.result.current.summary?.title).toBe("saved");
  });

  it("missing DONE remains unconfirmed; empty precise lookup cannot unlock retry", async () => {
    let turnId = "";
    const fetch = vi.fn((path: string): Promise<Response> => {
      if (path === "/conversations") return Promise.resolve(json(SUMMARY,201));
      if (path.startsWith("/query/stream")) {
        turnId = new URL(path,"https://workspace.test").searchParams.get("client_turn_id") ?? "";
        return Promise.resolve(new Response('data: {"event":"chunk","done":false,"chunk":"partial"}\n\n'));
      }
      return Promise.resolve(json({items:[],next_before:null}));
    });
    vi.stubGlobal("fetch",fetch);
    const hook = renderHook(() => useConversation("rag",true));
    await act(() => hook.result.current.send("synthetic"));
    expect(turnId).not.toBe("");
    expect(hook.result.current.unconfirmed).toBe(true);
    await act(() => hook.result.current.reconcile());
    expect(hook.result.current.unconfirmed).toBe(true);
    await act(() => hook.result.current.send("never replay"));
    expect(fetch.mock.calls.filter(([path]) => path.startsWith("/query/stream"))).toHaveLength(1);
    expect(fetch.mock.calls.some(([path]) => path.includes(`client_turn_id=${turnId}`) && path.includes("/turns"))).toBe(true);
  });

  it("explains unconfirmed saving in the real App and keeps submission locked", async () => {
    window.history.replaceState({}, "", "/");
    const fetch = vi.fn((path: string): Promise<Response> => {
      if (path === "/ready") return Promise.resolve(json({ status: "ready" }));
      if (path.startsWith("/query/stream")) return Promise.resolve(new Response('data: {"event":"chunk","done":false,"chunk":"partial"}\n\n'));
      if (path.startsWith("/conversations?")) return Promise.resolve(json({ items: [], next_cursor: null }));
      return Promise.resolve(json(SUMMARY, 201));
    });
    vi.stubGlobal("fetch", fetch);
    render(<App account={USER} />);
    const input = screen.getByRole("textbox", { name: "向知识库提问" });
    fireEvent.change(input, { target: { value: "synthetic" } });
    fireEvent.click(screen.getByRole("button", { name: /^发送$/ }));
    const locked = await screen.findByRole("button", { name: "本轮保存状态未确认，请先核对历史" });
    expect(locked).toBeDisabled();
    expect(input).toHaveAttribute("readonly");
    expect(input).toHaveAccessibleDescription("本轮保存状态未确认，请先核对历史");
    expect(screen.getByRole("button", { name: "核对本轮历史" })).toBeEnabled();
    expect(screen.queryByText("正在新建对话，暂不可发送")).not.toBeInTheDocument();
    fireEvent.keyDown(input, { key: "Enter" });
    expect(fetch.mock.calls.filter(([path]) => path.startsWith("/query/stream"))).toHaveLength(1);
  });

  it("route stop aborts delayed history lookup and prevents late UI writes", async () => {
    let resolve: ((response: Response) => void) | undefined;
    let signal: AbortSignal | undefined;
    vi.stubGlobal("fetch",vi.fn((path: string,init?: RequestInit): Promise<Response> => {
      signal = init?.signal ?? undefined;
      return path.endsWith("/turns") ? new Promise((done) => {resolve=done;}) : Promise.resolve(json(SUMMARY));
    }));
    const hook = renderHook(() => useConversation("rag",true));
    let opening: Promise<void> | undefined;
    act(() => {opening=hook.result.current.open(SESSION);});
    await waitFor(() => expect(resolve).toBeDefined());
    act(() => hook.result.current.stop(true));
    expect(signal?.aborted).toBe(true);
    await act(async () => {resolve?.(json({items:[],next_before:null}));await opening;});
    expect(hook.result.current.summary).toBeNull();
    expect(hook.result.current.resetting).toBe(false);
  });

  it("creation uses browser IDs and strict directory request bodies", async () => {
    const fetch = vi.fn().mockResolvedValue(json(SUMMARY,201)); vi.stubGlobal("fetch",fetch);
    await createConversation(SESSION,"rag",new AbortController().signal);
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({id:SESSION,mode:"rag"});
  });
});
