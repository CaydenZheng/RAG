import { afterEach, describe, expect, it, vi } from "vitest";
import {
  fetchPendingInteractions, loadOrCreateSessionId, postInteractionDecision,
  resetAndRotateSession, streamAgentAnswer, streamRagAnswer,
} from "../demo/client";
import type { StreamUpdate } from "../api/types";

afterEach(() => vi.useRealTimers());

describe("network-free public demo", () => {
  it("streams labelled RAG examples without network or persistent content", async () => {
    vi.useFakeTimers();
    const network = vi.spyOn(globalThis, "fetch").mockRejectedValue(new Error("Network forbidden"));
    const updates: StreamUpdate[] = [];
    const completion = streamRagAnswer("private-input", "demo", new AbortController().signal, (update) => updates.push(update));
    await vi.runAllTimersAsync();
    const result = await completion;
    expect(result.answer).toContain("预设模拟回答");
    expect(result.answer).not.toContain("private-input");
    expect(result.sources).toHaveLength(2);
    expect(updates.filter((update) => update.type === "chunk").length).toBeGreaterThan(1);
    expect(network).not.toHaveBeenCalled();
    expect(localStorage.length).toBe(0);
    network.mockRestore();
  });

  it.each(["审批", "表单", "URL"])("settles %s only after an explicit decision", async (message) => {
    vi.useFakeTimers();
    const controller = new AbortController();
    const completion = streamAgentAnswer(message, message, controller.signal, vi.fn());
    await vi.advanceTimersByTimeAsync(400);
    const [interaction] = await fetchPendingInteractions(message, controller.signal);
    expect(interaction).toBeDefined();
    await postInteractionDecision(message, interaction.kind === "approval"
      ? { kind: "approval", id: interaction.id, action: "approve" }
      : interaction.kind === "form"
        ? { kind: "form", id: interaction.id, action: "accept", content: { topic: "private-input" } }
        : { kind: "url", id: interaction.id, action: "accept" }, controller.signal);
    await vi.runAllTimersAsync();
    expect((await completion).answer).not.toContain("private-input");
    expect(await fetchPendingInteractions(message, controller.signal)).toEqual([]);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("cancels a pending interaction and clears its timers", async () => {
    vi.useFakeTimers();
    const controller = new AbortController();
    const completion = streamAgentAnswer("审批", "cancel", controller.signal, vi.fn());
    const failed = expect(completion).rejects.toMatchObject({ name: "AbortError" });
    await vi.advanceTimersByTimeAsync(400);
    controller.abort();
    await failed;
    expect(await fetchPendingInteractions("cancel", new AbortController().signal)).toEqual([]);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("fails at 25 seconds instead of inventing a successful result", async () => {
    vi.useFakeTimers();
    const completion = streamAgentAnswer("超时", "timeout", new AbortController().signal, vi.fn());
    const failed = expect(completion).rejects.toMatchObject({ code: "terminal_error", message: expect.stringContaining("25 秒") });
    await vi.advanceTimersByTimeAsync(25_200);
    await failed;
    expect(vi.getTimerCount()).toBe(0);
  });

  it("rotates memory-only session IDs without a backend reset", async () => {
    const oldId = loadOrCreateSessionId("rag");
    expect(await resetAndRotateSession("rag", oldId)).not.toBe(oldId);
    expect(localStorage.length).toBe(0);
  });
});
