import { describe, expect, it, vi } from "vitest";

import { streamAgentAnswer } from "../api/agentClient";
import { PublicClientError } from "../api/errors";
import { streamRagAnswer } from "../api/ragClient";
import { readSseEvents } from "../api/sse";

const encoder = new TextEncoder();

function sseResponse(chunks: string[], status = 200): Response {
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      chunks.forEach((chunk) => controller.enqueue(encoder.encode(chunk)));
      controller.close();
    },
  });
  return new Response(body, {
    status,
    headers: { "Content-Type": "text/event-stream" },
  });
}

describe("SSE framing", () => {
  it("yields the first event before the upstream stream completes", async () => {
    let closeStream: (() => void) | undefined;
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoder.encode('data: {"chunk":"首段"}\n\n'));
        closeStream = () => controller.close();
      },
    });
    const controller = new AbortController();
    const events = readSseEvents(body, controller.signal);

    await expect(events.next()).resolves.toEqual({
      done: false,
      value: { chunk: "首段" },
    });
    closeStream?.();
    await events.return(undefined);
  });

  it("parses JSON split across chunks and CRLF event boundaries", async () => {
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoder.encode('retry: 3000\r\n\r\ndata: {"event":"ch'));
        controller.enqueue(encoder.encode('unk","chunk":"A","done":false}\r\n\r\n'));
        controller.close();
      },
    });
    const events: unknown[] = [];
    for await (const event of readSseEvents(body, new AbortController().signal)) {
      events.push(event);
    }
    expect(events).toEqual([{ event: "chunk", chunk: "A", done: false }]);
  });
});

describe("RAG stream", () => {
  it("exposes chunks and accepts one matching DONE terminal", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        sseResponse([
          'data: {"event":"chunk","done":false,"chunk":"答"}\n\n',
          'data: {"event":"chunk","done":false,"chunk":"案"}\n\n',
          'data: {"event":"done","done":true,"answer":"答案","sources":[],"warnings":[],"index_version":"v1","latency_ms":12}\n\n',
          'data: {"event":"done","done":true,"answer":"迟到"}\n\n',
        ]),
      ),
    );
    const chunks: string[] = [];
    const completion = await streamRagAnswer(
      "问题",
      "session-1",
      new AbortController().signal,
      (update) => {
        if (update.type === "chunk") chunks.push(update.chunk);
      },
    );
    expect(chunks).toEqual(["答", "案"]);
    expect(completion.answer).toBe("答案");
    expect(completion.indexVersion).toBe("v1");
  });

  it.each([
    [422, "输入内容不符合要求，请检查后重试。"],
    [503, "服务尚未就绪，请稍后重试。"],
  ])("handles HTTP %s before reading a stream", async (status, message) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status })));
    await expect(
      streamRagAnswer("问题", "session-1", new AbortController().signal, vi.fn()),
    ).rejects.toMatchObject({ message });
  });

  it("rejects ERROR without returning a successful answer", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        sseResponse([
          'data: {"event":"chunk","done":false,"chunk":"半截"}\n\n',
          'data: {"event":"error","done":true,"error":{"code":"answer_generation_failed","message":"internal"}}\n\n',
        ]),
      ),
    );
    await expect(
      streamRagAnswer("问题", "session-1", new AbortController().signal, vi.fn()),
    ).rejects.toMatchObject({ code: "terminal_error" });
  });

  it("rejects a disconnect without a terminal event", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        sseResponse(['data: {"event":"chunk","done":false,"chunk":"半截"}\n\n']),
      ),
    );
    await expect(
      streamRagAnswer("问题", "session-1", new AbortController().signal, vi.fn()),
    ).rejects.toBeInstanceOf(PublicClientError);
  });

  it("rejects DONE when streamed chunks do not match the final answer", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        sseResponse([
          'data: {"event":"chunk","done":false,"chunk":"A"}\n\n',
          'data: {"event":"done","done":true,"answer":"B"}\n\n',
        ]),
      ),
    );
    await expect(
      streamRagAnswer("问题", "session-1", new AbortController().signal, vi.fn()),
    ).rejects.toMatchObject({ code: "protocol" });
  });
});

describe("Agent stream", () => {
  it("parses tool progress, chunks, and a matching DONE terminal", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        sseResponse([
          'data: {"step":"planning","iteration":1}\n\n',
          'data: {"step":"tool_call","iteration":1,"tool":"calculator","call_id":"c1","params":{"expression":"1+1"}}\n\n',
          'data: {"step":"tool_done","iteration":1,"tool":"calculator","call_id":"c1","success":true}\n\n',
          'data: {"chunk":"2"}\n\n',
          'data: {"done":true,"answer":"2","iterations":1,"latency_ms":8}\n\n',
        ]),
      ),
    );
    const updates: string[] = [];
    const completion = await streamAgentAnswer(
      "计算",
      "agent-1",
      new AbortController().signal,
      (update) => updates.push(update.type),
    );
    expect(updates).toEqual(["tool_step", "tool_step", "tool_step", "chunk"]);
    expect(completion).toMatchObject({ answer: "2", iterations: 1, latencyMs: 8 });
  });

  it("cancels the reader when the request signal is aborted", async () => {
    const cancel = vi.fn();
    const body = new ReadableStream<Uint8Array>({ cancel });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(body, { status: 200 })));
    const controller = new AbortController();
    const result = streamAgentAnswer("任务", "agent-1", controller.signal, vi.fn());
    await Promise.resolve();
    controller.abort();
    await expect(result).rejects.toBeInstanceOf(PublicClientError);
    expect(cancel).toHaveBeenCalledOnce();
  });
});
