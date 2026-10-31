import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { useReducer } from "react";
import { AttachmentPanel } from "../components/AttachmentPanel";
import { KnowledgePage } from "../components/KnowledgePage";
import { ConversationView } from "../components/ConversationView";
import { conversationReducer, initialConversationState } from "../app/conversationState";

const FILE = {id:"file-a",client_upload_id:"upload-a",name:"notes.txt",suffix:"txt",bytes:20,checksum:"a".repeat(64),state:"ready",error_code:null,page_count:0};
function json(value: unknown): Response { return new Response(JSON.stringify(value), {headers:{"Content-Type":"application/json"}}); }

describe("conversation attachments", () => {
  it("marks current completed answers deleted only after successful attachment deletion without replacing their text or citations", async () => {
    let finish: ((response:Response) => void) | undefined;
    let deleted = false;
    vi.stubGlobal("confirm", vi.fn(() => true));
    vi.stubGlobal("fetch", vi.fn((_path: string, init?: RequestInit): Promise<Response> => {
      if (init?.method === "DELETE") return new Promise(resolve => {finish = response => { deleted = true; resolve(response); };});
      return Promise.resolve(json({items:deleted ? [] : [FILE]}));
    }));
    const completed = conversationReducer(conversationReducer(initialConversationState, {type:"start", question:"Synthetic question"}), {
      type:"complete_rag", completion:{answer:"Preserved answer [1]", sources:[{ref:"1",source:"notes.txt",text:"Preserved evidence",score:null}],warnings:[],latencyMs:1,indexVersion:"attachments",
        attachments:[{id:FILE.id,name:FILE.name,checksum:FILE.checksum}]},
    });
    function Harness(): React.JSX.Element {
      const [state, dispatch] = useReducer(conversationReducer, completed);
      return <><ConversationView mode="rag" state={state} onInteractionDecision={vi.fn()} />
        <AttachmentPanel sessionId="conv-a" exists disabled={false} ensure={vi.fn()} onSelection={vi.fn()}
          onDeleted={id => dispatch({type:"attachment_deleted", id})} /></>;
    }
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(await screen.findByRole("button", {name:"删除 notes.txt"}));
    expect(screen.queryByText(/附件 \[1\].*已删除/)).not.toBeInTheDocument();
    await act(async () => finish?.(new Response(null, {status:204})));
    await waitFor(() => expect(screen.getByText("附件 [1] notes.txt（已删除）")).toBeVisible());
    expect(screen.getByText(/Preserved answer/)).toBeVisible();
    await user.click(screen.getByText("引用来源 · 1"));
    expect(screen.getByText("Preserved evidence")).toBeVisible();
  });

  it("uploads to the conversation, waits for parsing, selects explicitly and deletes separately", async () => {
    let uploaded = false;
    let reads = 0;
    vi.stubGlobal("confirm", vi.fn(() => true));
    const fetch = vi.fn((path: string, init?: RequestInit): Promise<Response> => {
      expect(path).toBe("/conversations/conv-a/attachments" + (init?.method === "DELETE" ? "/file-a" : ""));
      if (init?.method === "POST") { uploaded = true; return Promise.resolve(json({...FILE,state:"parsing"})); }
      if (init?.method === "DELETE") { uploaded = false; return Promise.resolve(new Response(null, {status:204})); }
      reads += 1;
      return Promise.resolve(json({items:uploaded ? [FILE] : []}));
    });
    vi.stubGlobal("fetch", fetch);
    const select = vi.fn();
    const user = userEvent.setup();
    render(<AttachmentPanel sessionId="conv-a" exists disabled={false} ensure={vi.fn()} onSelection={select} />);
    await waitFor(() => expect(reads).toBe(1));
    await user.upload(screen.getByLabelText("添加聊天附件"), new File(["public notes"], "notes.txt", {type:"text/plain"}));
    const checkbox = await screen.findByRole("checkbox", {name:"本轮使用 notes.txt"});
    await waitFor(() => expect(checkbox).toBeEnabled());
    expect(checkbox).toBeChecked();
    expect(select).toHaveBeenLastCalledWith(["file-a"], false);
    await user.click(checkbox);
    expect(select).toHaveBeenLastCalledWith([], false);
    expect(fetch.mock.calls.some(([, init]) => init?.method === "DELETE")).toBe(false);
    await user.click(screen.getByRole("button", {name:"删除 notes.txt"}));
    await waitFor(() => expect(checkbox).not.toBeInTheDocument());
    expect(fetch.mock.calls.some(([path, init]) => path === "/upload" || init?.method === "PATCH")).toBe(false);
  });

  it("retries a lost upload response with the original id and unchanged file", async () => {
    const uploadIds: unknown[] = [];
    const uploadedFiles: unknown[] = [];
    const fetch = vi.fn((_path: string, init?: RequestInit): Promise<Response> => {
      if (init?.method === "POST" && init.body instanceof FormData) {
        uploadIds.push(init.body.get("client_upload_id")); uploadedFiles.push(init.body.get("file"));
        if (uploadIds.length === 1) return Promise.reject(new TypeError("PRIVATE_TRANSPORT_DETAIL"));
        return Promise.resolve(json(FILE));
      }
      return Promise.resolve(json({items:[]}));
    });
    vi.stubGlobal("fetch", fetch);
    const user = userEvent.setup();
    render(<AttachmentPanel sessionId="conv-a" exists disabled={false} ensure={vi.fn()} onSelection={vi.fn()} />);
    const file = new File(["public"], "notes.txt", {type:"text/plain"});
    await user.upload(screen.getByLabelText("添加聊天附件"), file);
    await user.click(await screen.findByRole("button", {name:"核对／重试本次上传"}));
    await waitFor(() => expect(uploadIds).toHaveLength(2));
    expect(uploadIds[0]).toBe(uploadIds[1]);
    expect(uploadedFiles).toEqual([file,file]);
    expect(screen.queryByText("PRIVATE_TRANSPORT_DETAIL")).not.toBeInTheDocument();
  });

  it("aborts catalog polling and an unfinished upload on unmount", async () => {
    const signals: AbortSignal[] = [];
    let accept: ((response:Response) => void) | undefined;
    vi.stubGlobal("fetch", vi.fn((_path: string, init?: RequestInit): Promise<Response> => {
      if (init?.signal) signals.push(init.signal);
      if (init?.method === "POST") return new Promise(resolve => {accept = resolve;});
      return Promise.resolve(json({items:[]}));
    }));
    const user = userEvent.setup();
    const select = vi.fn();
    const view = render(<AttachmentPanel sessionId="conv-a" exists disabled={false} ensure={vi.fn()} onSelection={select} />);
    await user.upload(screen.getByLabelText("添加聊天附件"), new File(["notes"], "notes.txt", {type:"text/plain"}));
    await waitFor(() => expect(accept).toBeDefined());
    view.unmount();
    expect(signals.every(signal => signal.aborted)).toBe(true);
    const count = select.mock.calls.length;
    await act(async () => accept?.(json(FILE)));
    expect(select).toHaveBeenCalledTimes(count);
  });
});

describe("read-only knowledge", () => {
  it("searches by POST and shows a bounded, inert source excerpt", async () => {
    const document = {id:"doc-a",name:"handbook.md",checksum:"b".repeat(64),index_version:"version-a",indexed_at:"2026-10-30",updated_at:null};
    const fetch = vi.fn((path: string, _init?: RequestInit): Promise<Response> => Promise.resolve(json(path.includes("/doc-a?")
      ? {id:"doc-a",name:document.name,text:'<script>BAD()</script> https://private.test/?token=synthetic',excerpt_only:true}
      : {items:[document],next_cursor:null,index_version:"version-a"})));
    vi.stubGlobal("fetch", fetch);
    const user = userEvent.setup();
    render(<KnowledgePage available />);
    await screen.findByText("handbook.md");
    await user.type(screen.getByLabelText("搜索文档名称"), "private search");
    await user.click(screen.getByRole("button", {name:/^搜索$/}));
    await waitFor(() => expect(fetch.mock.calls.some(([path]) => path === "/knowledge/documents/search")).toBe(true));
    const call = fetch.mock.calls.find(([path]) => path === "/knowledge/documents/search");
    expect(call?.[1]).toMatchObject({method:"POST", body:JSON.stringify({title_query:"private search"})});
    await user.click(screen.getByRole("button", {name:"查看来源摘录：handbook.md"}));
    const excerpt = await screen.findByRole("region", {name:"来源摘录"});
    expect(within(excerpt).getByText(/以下仅为/)).toBeVisible();
    expect(excerpt.querySelector("script")).toBeNull();
    expect(excerpt.querySelector("a")).toBeNull();
    expect(fetch.mock.calls.every(([path]) => !path.includes("private search"))).toBe(true);
  });
});
