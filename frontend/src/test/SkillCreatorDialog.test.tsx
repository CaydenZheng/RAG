import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createHash, webcrypto } from "node:crypto";
import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";

import { SkillsPage } from "../components/SkillsPage";
import type { ModelOption } from "../api/workspaceClient";

const MODELS: ModelOption[] = [
  {id:"flash", label:"Flash", available:true}, {id:"pro", label:"Pro", available:true},
];
const CREATOR = {id:"skill-creator", name:"skill-creator", description:"Convert documents", builtin:true, enabled:true, revision:1, version:"a".repeat(64)};
const CONTENT = "---\nschema_version: 1\nname: concise\ndescription: Concise writing\n---\nBe concise.\n";
const CHECKSUM = createHash("sha256").update(CONTENT).digest("hex");
const CUSTOM = {id:"existing", name:"concise", description:"Concise writing", builtin:false, enabled:true, revision:1, version:CHECKSUM};
const terminal = {done:true, filename:"concise.md", content:CONTENT, checksum:CHECKSUM};
const encoder = new TextEncoder();
function frame(value: unknown): Uint8Array { return encoder.encode(`data: ${JSON.stringify(value)}\n\n`); }
function json(value: unknown): Response { return new Response(JSON.stringify(value), {headers:{"Content-Type":"application/json"}}); }

function artifactUrls(): ReturnType<typeof vi.fn> {
  const revoke = vi.fn();
  class BrowserUrl extends URL {
    static createObjectURL = vi.fn(() => "blob:synthetic-artifact");
    static revokeObjectURL = revoke;
  }
  vi.stubGlobal("URL", BrowserUrl);
  return revoke;
}

// jsdom does not implement native dialog lifecycle; browser tests cover focus,
// Escape and the actual modal top layer.
const originalShow = Object.getOwnPropertyDescriptor(HTMLDialogElement.prototype, "showModal");
const originalClose = Object.getOwnPropertyDescriptor(HTMLDialogElement.prototype, "close");
beforeAll(() => {
  Object.defineProperties(HTMLDialogElement.prototype, {
    showModal: {configurable:true, value(this: HTMLDialogElement): void { this.open = true; }},
    close: {configurable:true, value(this: HTMLDialogElement): void { this.open = false; this.dispatchEvent(new Event("close")); }},
  });
});
afterAll(() => {
  if (originalShow) Object.defineProperty(HTMLDialogElement.prototype, "showModal", originalShow);
  else Reflect.deleteProperty(HTMLDialogElement.prototype, "showModal");
  if (originalClose) Object.defineProperty(HTMLDialogElement.prototype, "close", originalClose);
  else Reflect.deleteProperty(HTMLDialogElement.prototype, "close");
});

describe("restored creator dialog", () => {
  it("previews and verifies a real SSE artifact before download or explicit idempotent upload", async () => {
    vi.stubGlobal("crypto", webcrypto);
    const revoke = artifactUrls();
    let stream: ReadableStreamDefaultController<Uint8Array> | undefined;
    const fetch = vi.fn((path: string, init?: RequestInit): Promise<Response> => {
      if (path === "/skills/creator/stream") return Promise.resolve(new Response(new ReadableStream<Uint8Array>({
        start(controller) { stream = controller; controller.enqueue(frame({chunk:CONTENT})); },
      }), {headers:{"Content-Type":"text/event-stream"}}));
      if (path === "/skills" && init?.method === "POST") return Promise.resolve(json(CUSTOM));
      return Promise.resolve(json({items:[CREATOR, CUSTOM]}));
    });
    vi.stubGlobal("fetch", fetch);
    const user = userEvent.setup();
    render(<SkillsPage available models={MODELS} defaultModel="pro" />);
    const open = await screen.findByRole("button", {name:"从资料生成技能"});
    await waitFor(() => expect(open).toBeEnabled());
    await user.click(open);
    const dialog = screen.getByRole("dialog", {name:"从资料生成技能"});
    expect(within(dialog).getByRole("combobox", {name:"模型"})).toHaveValue("pro");
    await user.upload(within(dialog).getByLabelText("导入原始资料"), new File(["public notes"], "notes.txt", {type:"text/plain"}));
    await user.click(within(dialog).getByRole("button", {name:"开始整理"}));
    await waitFor(() => expect(within(dialog).getByLabelText("技能转换预览")).toHaveTextContent("Be concise."));
    expect(within(dialog).queryByRole("link", {name:"下载技能文件"})).not.toBeInTheDocument();
    expect(fetch.mock.calls.filter(([path, init]) => path === "/skills" && init?.method === "POST")).toHaveLength(0);
    await act(async () => { stream?.enqueue(frame(terminal)); stream?.close(); });
    const download = await within(dialog).findByRole("link", {name:"下载技能文件"});
    expect(download).toHaveAttribute("download", "concise.md");
    const request = fetch.mock.calls.find(([path]) => path === "/skills/creator/stream");
    const form = request?.[1]?.body;
    expect(form instanceof FormData && form.get("model")).toBe("pro");
    await user.click(within(dialog).getByRole("button", {name:"上传此技能（新项默认关闭）"}));
    await waitFor(() => expect(within(dialog).getByRole("status")).toHaveTextContent("当前启用"));
    expect(within(dialog).getByRole("status")).not.toHaveTextContent("当前关闭");
    expect(fetch.mock.calls.filter(([path, init]) => path === "/skills" && init?.method === "POST")).toHaveLength(1);
    expect(fetch.mock.calls.some(([, init]) => init?.method === "PATCH")).toBe(false);
    await user.click(within(dialog).getByRole("button", {name:"关闭生成窗口"}));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(revoke).toHaveBeenCalledWith("blob:synthetic-artifact");
  });

  it("invalidates the previous artifact when the source changes and only uploads the regenerated artifact", async () => {
    vi.stubGlobal("crypto", webcrypto);
    const revoke = artifactUrls();
    const nextContent = CONTENT.replaceAll("concise", "expanded");
    const nextTerminal = {done:true, filename:"expanded.md", content:nextContent, checksum:createHash("sha256").update(nextContent).digest("hex")};
    let generations = 0;
    const fetch = vi.fn((path: string, init?: RequestInit): Promise<Response> => {
      if (path === "/skills/creator/stream") {
        generations += 1;
        return Promise.resolve(new Response(`data: ${JSON.stringify(generations === 1 ? terminal : nextTerminal)}\n\n`, {headers:{"Content-Type":"text/event-stream"}}));
      }
      if (path === "/skills" && init?.method === "POST") return Promise.resolve(json(CUSTOM));
      return Promise.resolve(json({items:[CREATOR, CUSTOM]}));
    });
    vi.stubGlobal("fetch", fetch);
    const user = userEvent.setup();
    render(<SkillsPage available models={MODELS} />);
    const open = await screen.findByRole("button", {name:"从资料生成技能"});
    await waitFor(() => expect(open).toBeEnabled()); await user.click(open);
    const dialog = screen.getByRole("dialog", {name:"从资料生成技能"});
    const source = within(dialog).getByLabelText("导入原始资料");
    await user.upload(source, new File(["notes A"], "a.txt", {type:"text/plain"}));
    await user.click(within(dialog).getByRole("button", {name:"开始整理"}));
    expect(await within(dialog).findByRole("link", {name:"下载技能文件"})).toHaveAttribute("download", "concise.md");
    await user.upload(source, new File(["notes B"], "b.txt", {type:"text/plain"}));
    expect(within(dialog).queryByLabelText("技能转换预览")).not.toBeInTheDocument();
    expect(within(dialog).queryByRole("link", {name:"下载技能文件"})).not.toBeInTheDocument();
    expect(within(dialog).queryByRole("button", {name:"上传此技能（新项默认关闭）"})).not.toBeInTheDocument();
    expect(within(dialog).getByRole("status")).toBeEmptyDOMElement();
    expect(revoke).toHaveBeenCalledWith("blob:synthetic-artifact");
    expect(fetch.mock.calls.some(([path, init]) => path === "/skills" && init?.method === "POST")).toBe(false);
    await user.click(within(dialog).getByRole("button", {name:"开始整理"}));
    expect(await within(dialog).findByRole("link", {name:"下载技能文件"})).toHaveAttribute("download", "expanded.md");
    const request = fetch.mock.calls.filter(([path]) => path === "/skills/creator/stream")[1];
    const submittedSource = request[1]?.body instanceof FormData ? request[1].body.get("file") : null;
    expect(submittedSource instanceof File && submittedSource.name).toBe("b.txt");
    await user.click(within(dialog).getByRole("button", {name:"上传此技能（新项默认关闭）"}));
    await waitFor(() => expect(within(dialog).getByRole("status")).toHaveTextContent("当前启用"));
    const upload = fetch.mock.calls.find(([path, init]) => path === "/skills" && init?.method === "POST");
    const savedFile = upload?.[1]?.body instanceof FormData ? upload[1].body.get("file") : null;
    expect(savedFile instanceof File && savedFile.name).toBe("expanded.md");
    fireEvent.change(source, {target:{files:[]}});
    expect(within(dialog).queryByLabelText("技能转换预览")).not.toBeInTheDocument();
    expect(within(dialog).queryByRole("link", {name:"下载技能文件"})).not.toBeInTheDocument();
    expect(within(dialog).queryByRole("button", {name:"上传此技能（新项默认关闭）"})).not.toBeInTheDocument();
    expect(within(dialog).getByRole("status")).toBeEmptyDOMElement();
    expect(within(dialog).getByRole("button", {name:"开始整理"})).toBeDisabled();
  });

  it("leaves an interrupted upload unconfirmed and reconciles by reading instead of reposting", async () => {
    vi.stubGlobal("crypto", webcrypto); artifactUrls();
    let finish: ((response: Response) => void) | undefined;
    let signal: AbortSignal | undefined;
    const fetch = vi.fn((path: string, init?: RequestInit): Promise<Response> => {
      if (path === "/skills/creator/stream") return Promise.resolve(new Response(`data: ${JSON.stringify(terminal)}\n\n`, {headers:{"Content-Type":"text/event-stream"}}));
      if (path === "/skills" && init?.method === "POST") {
        signal = init.signal ?? undefined;
        return new Promise(resolve => { finish = resolve; });
      }
      return Promise.resolve(json({items:[CREATOR, CUSTOM]}));
    });
    vi.stubGlobal("fetch", fetch);
    const user = userEvent.setup();
    render(<SkillsPage available models={MODELS} />);
    const open = await screen.findByRole("button", {name:"从资料生成技能"});
    await waitFor(() => expect(open).toBeEnabled()); await user.click(open);
    const dialog = screen.getByRole("dialog", {name:"从资料生成技能"});
    await user.upload(within(dialog).getByLabelText("导入原始资料"), new File(["notes"], "notes.txt", {type:"text/plain"}));
    await user.click(within(dialog).getByRole("button", {name:"开始整理"}));
    await within(dialog).findByRole("link", {name:"下载技能文件"});
    await user.click(within(dialog).getByRole("button", {name:"上传此技能（新项默认关闭）"}));
    await waitFor(() => expect(finish).toBeDefined());
    await user.click(within(dialog).getByRole("button", {name:"关闭生成窗口"}));
    expect(signal?.aborted).toBe(true);
    expect(screen.getByRole("alert")).toHaveTextContent("上传结果未确认");
    const callsAfterClose = fetch.mock.calls.length;
    await act(async () => { finish?.(json(CUSTOM)); });
    expect(fetch).toHaveBeenCalledTimes(callsAfterClose);
    await user.click(screen.getByRole("button", {name:"刷新目录"}));
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(callsAfterClose + 1));
    expect(fetch.mock.calls.filter(([path, init]) => path === "/skills" && init?.method === "POST")).toHaveLength(1);
  });

  it.each(["stop", "close", "unmount"] as const)("cancels the active reader on %s without a downloadable success", async (action) => {
    const cancel = vi.fn();
    let signal: AbortSignal | undefined;
    vi.stubGlobal("fetch", vi.fn((path: string, init?: RequestInit): Promise<Response> => {
      if (path !== "/skills/creator/stream") return Promise.resolve(json({items:[CREATOR]}));
      signal = init?.signal ?? undefined;
      return Promise.resolve(new Response(new ReadableStream<Uint8Array>({
        start(controller) { controller.enqueue(frame({chunk:"unverified preview"})); }, cancel,
      }), {headers:{"Content-Type":"text/event-stream"}}));
    }));
    const user = userEvent.setup();
    const view = render(<SkillsPage available models={MODELS} />);
    const open = await screen.findByRole("button", {name:"从资料生成技能"});
    await waitFor(() => expect(open).toBeEnabled()); await user.click(open);
    const dialog = screen.getByRole("dialog", {name:"从资料生成技能"});
    await user.upload(within(dialog).getByLabelText("导入原始资料"), new File(["notes"], "notes.txt", {type:"text/plain"}));
    await user.click(within(dialog).getByRole("button", {name:"开始整理"}));
    await within(dialog).findByText("unverified preview");
    if (action === "unmount") view.unmount();
    else fireEvent.click(within(dialog).getByRole("button", {name:action === "stop" ? "停止转换" : "关闭生成窗口"}));
    await waitFor(() => expect(signal?.aborted).toBe(true));
    await waitFor(() => expect(cancel).toHaveBeenCalled());
    expect(screen.queryByRole("link", {name:"下载技能文件"})).not.toBeInTheDocument();
  });

  it("keeps a preview without DONE unverified and never offers upload", async () => {
    vi.stubGlobal("fetch", vi.fn((path: string): Promise<Response> => Promise.resolve(path === "/skills"
      ? json({items:[CREATOR]})
      : new Response(new ReadableStream<Uint8Array>({start(controller) { controller.enqueue(frame({chunk:CONTENT})); controller.close(); }}),
        {headers:{"Content-Type":"text/event-stream"}}))));
    const user = userEvent.setup();
    render(<SkillsPage available models={MODELS} />);
    const open = await screen.findByRole("button", {name:"从资料生成技能"});
    await waitFor(() => expect(open).toBeEnabled()); await user.click(open);
    const dialog = screen.getByRole("dialog", {name:"从资料生成技能"});
    await user.upload(within(dialog).getByLabelText("导入原始资料"), new File(["notes"], "notes.txt", {type:"text/plain"}));
    await user.click(within(dialog).getByRole("button", {name:"开始整理"}));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("完成状态未确认");
    expect(within(dialog).queryByRole("button", {name:"上传此技能（新项默认关闭）"})).not.toBeInTheDocument();
  });
});
