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
  it("ignores unknown events, exposes chunks, and accepts one matching DONE terminal", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        sseResponse([
          'data: {"unknown":"ignored"}\n\n',
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
  it("accepts the successful AgentResponse serializer shape including empty errors", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(sseResponse([
      'data: {"chunk":"complete"}\n\n',
      'data: {"done":true,"session_id":"agent-1","answer":"complete","tool_calls":[],"iterations":0,"latency_ms":0,"error_code":"","error":""}\n\n',
    ])));
    await expect(streamAgentAnswer("question", "agent-1", new AbortController().signal, vi.fn()))
      .resolves.toMatchObject({ answer: "complete" });
  });

  it.each([
    { done: true, answer: "complete", error: "private failure", error_code: "agent_failed" },
    { done: true, answer: "complete", error: "", error_code: "agent_failed" },
    { error: "", code: "agent_failed" },
  ])("rejects an explicit Agent failure even with matching answer chunks: %j", async (terminal) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(sseResponse([
      'data: {"chunk":"complete"}\n\n',
      `data: ${JSON.stringify(terminal)}\n\n`,
    ])));
    await expect(streamAgentAnswer("question", "agent-1", new AbortController().signal, vi.fn()))
      .rejects.toMatchObject({ code: "terminal_error", message: "Agent 处理失败，请稍后重试。" });
  });

  it("keeps a failed tool result as progress when the Agent completes successfully", async () => {
    const updates: string[] = [];
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(sseResponse([
      'data: {"step":"tool_done","tool":"calculator","success":false,"error_code":"tool_failed"}\n\n',
      'data: {"chunk":"complete"}\n\n',
      'data: {"done":true,"answer":"complete","error_code":"","error":""}\n\n',
    ])));
    await expect(streamAgentAnswer("question", "agent-1", new AbortController().signal,
      (update) => updates.push(update.type))).resolves.toMatchObject({ answer: "complete" });
    expect(updates).toEqual(["tool_step", "chunk"]);
  });

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
          'data: {"done":true,"answer":"late"}\n\n',
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

  it.each([
    [422, "输入内容不符合要求，请检查后重试。"],
    [503, "服务尚未就绪，请稍后重试。"],
  ])("handles HTTP %s before reading an Agent stream", async (status, message) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status })));
    await expect(
      streamAgentAnswer("任务", "agent-1", new AbortController().signal, vi.fn()),
    ).rejects.toMatchObject({ message });
  });

  it("rejects an Agent ERROR without returning a successful answer", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        sseResponse([
          'data: {"chunk":"半截"}\n\n',
          'data: {"error":{"code":"agent_failed","message":"internal"}}\n\n',
        ]),
      ),
    );
    await expect(
      streamAgentAnswer("任务", "agent-1", new AbortController().signal, vi.fn()),
    ).rejects.toMatchObject({ code: "terminal_error" });
  });

  it("rejects an Agent disconnect without a terminal event", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(sseResponse(['data: {"chunk":"半截"}\n\n'])),
    );
    await expect(
      streamAgentAnswer("任务", "agent-1", new AbortController().signal, vi.fn()),
    ).rejects.toMatchObject({ code: "disconnected" });
  });

  it.each([
    ["agent_planning_incomplete", "规划输出未完整结束，请精简任务后重试。"],
    ["agent_planning_upstream_failed", "规划服务暂时不可用，请稍后重试。"],
    ["agent_final_generation_failed", "回答生成未完成，本轮未保存为成功；请精简任务后重试。"],
    ["invalid_agent_plan", "规划格式无效，本轮任务未完成，请调整任务后重试。"],
    ["agent_token_budget_exceeded", "本轮任务已达到处理预算，请拆分任务后重试。"],
    ["agent_repeated_tool_call", "规划持续请求重复工具，已停止本轮任务；请核对已有工具结果后调整任务。"],
    ["agent_timeout", "任务处理超时，请精简任务后重试。"],
  ])("projects %s into fixed public feedback", async (code, message) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(sseResponse([
      `data: ${JSON.stringify({ error: "PRIVATE_PROVIDER_ERROR", code })}\n\n`,
    ])));
    await expect(streamAgentAnswer("task", "agent-1", new AbortController().signal, vi.fn()))
      .rejects.toMatchObject({ code: "terminal_error", message });
  });

  it("rejects Agent DONE when chunks do not match the final answer", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        sseResponse([
          'data: {"chunk":"A"}\n\n',
          'data: {"done":true,"answer":"B"}\n\n',
        ]),
      ),
    );
    await expect(
      streamAgentAnswer("任务", "agent-1", new AbortController().signal, vi.fn()),
    ).rejects.toMatchObject({ code: "protocol" });
  });
});
