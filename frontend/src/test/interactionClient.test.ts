import { describe, expect, it, vi } from "vitest";

import {
  fetchPendingInteractions,
  postInteractionDecision,
} from "../api/interactionClient";
import { safeExternalUrl } from "../api/safeUrls";
import type { InteractionDecision } from "../api/types";

const RESPONSE_LOSS_DECISIONS: readonly InteractionDecision[] = [
  { kind: "approval", id: "approval-1", action: "approve" },
  { kind: "form", id: "form-1", action: "accept", content: { answer: "yes" } },
  { kind: "url", id: "url-1", action: "accept" },
];

describe("Agent interaction client", () => {
  it("reads only the documented Approval and form/url Elicitation fields", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ pending: [{
        id: "approval-1",
        tool_name: "send_message",
        source: "native",
        provider: null,
        category: "external",
        params: { message: "private value" },
        security_level: "critical",
      }] }), { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ pending: [
        {
          id: "form-1",
          server_id: "interactive",
          mode: "form",
          message: "Choose an answer",
          requested_schema: {
            type: "object",
            properties: { answer: { title: "Answer", type: "string" } },
            required: ["answer"],
          },
        },
        {
          id: "url-1",
          server_id: "oauth-provider",
          mode: "url",
          message: "Complete sign-in",
          url: "https://login.example.test/authorize?token=secret",
          elicitation_id: "opaque-id",
        },
      ] }), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    const controller = new AbortController();

    const result = await fetchPendingInteractions("session-1", controller.signal);

    expect(result).toHaveLength(3);
    expect(result[0]).toEqual(expect.objectContaining({
      kind: "approval",
      id: "approval-1",
      toolName: "send_message",
      source: "native",
      provider: null,
      category: "external",
      params: { message: "private value" },
    }));
    expect(result[0]).not.toHaveProperty("securityLevel");
    expect(result[1]).toEqual(expect.objectContaining({ kind: "form", id: "form-1" }));
    expect(result[2]).toEqual(expect.objectContaining({
      kind: "url",
      id: "url-1",
      url: "https://login.example.test/authorize?token=secret",
    }));
    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "/agent/approvals/session-1",
      expect.objectContaining({ signal: controller.signal }),
    );
  });

  it("posts form content only for accepted form requests", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 202 }));
    vi.stubGlobal("fetch", fetchMock);

    await postInteractionDecision(
      "session-1",
      { kind: "form", id: "form-1", action: "accept", content: { answer: "yes" } },
      new AbortController().signal,
    );

    expect(fetchMock).toHaveBeenCalledWith(
      "/agent/elicitation/session-1/form-1",
      expect.objectContaining({ body: JSON.stringify({ action: "accept", content: { answer: "yes" } }) }),
    );
  });

  it("never adds content to Approval or URL decisions", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 202 }));
    vi.stubGlobal("fetch", fetchMock);
    const signal = new AbortController().signal;

    await postInteractionDecision(
      "session-1",
      { kind: "approval", id: "approval-1", action: "reject" },
      signal,
    );
    await postInteractionDecision(
      "session-1",
      { kind: "url", id: "url-1", action: "accept" },
      signal,
    );

    expect((fetchMock.mock.calls[0][1] as RequestInit).body).toBe(JSON.stringify({ action: "reject" }));
    expect((fetchMock.mock.calls[1][1] as RequestInit).body).toBe(JSON.stringify({ action: "accept" }));
  });

  it.each([
    [400, "interaction_invalid"],
    [404, "interaction_closed"],
    [413, "interaction_too_large"],
    [503, "interaction_outcome_unknown"],
  ] as const)("maps response status %s without exposing a response body", async (status, code) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(
      new Response("private backend detail", { status }),
    ));

    await expect(postInteractionDecision(
      "session-1",
      { kind: "approval", id: "approval-1", action: "approve" },
      new AbortController().signal,
    )).rejects.toMatchObject({ code });
  });

  it.each(RESPONSE_LOSS_DECISIONS)(
    "treats an unconfirmed $kind response as outcome unknown",
    async (decision) => {
      vi.stubGlobal("fetch", vi.fn().mockRejectedValue(
        new TypeError("private transport detail"),
      ));

      await expect(postInteractionDecision(
        "session-1",
        decision,
        new AbortController().signal,
      )).rejects.toMatchObject({
        code: "interaction_outcome_unknown",
        message: "未收到提交确认，请核对工具执行结果后再继续。",
      });
    },
  );

  it("allows safe external protocols without exposing query details in its summary", () => {
    expect(safeExternalUrl("https://login.example.test/path?token=secret")).toEqual({
      href: "https://login.example.test/path?token=secret",
      hostname: "login.example.test",
    });
    expect(safeExternalUrl("http://127.0.0.1:8765/callback?code=secret")?.hostname).toBe("127.0.0.1");
    expect(safeExternalUrl("http://example.test/callback")).toBeNull();
    expect(safeExternalUrl("javascript:alert(1)")).toBeNull();
  });

  it("does not turn a failed pending poll into an empty successful snapshot", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("private detail", { status: 503 })));

    await expect(fetchPendingInteractions(
      "session-1",
      new AbortController().signal,
    )).rejects.toMatchObject({ code: "interaction_unavailable" });
  });

  it("aborts the other pending request when one request fails", async () => {
    let hangingSignal: AbortSignal | null = null;
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).startsWith("/agent/approvals/")) {
        return Promise.resolve(new Response("private detail", { status: 503 }));
      }
      hangingSignal = init?.signal as AbortSignal;
      return new Promise<Response>((_resolve, reject) => {
        hangingSignal?.addEventListener("abort", () => {
          reject(new DOMException("aborted", "AbortError"));
        }, { once: true });
      });
    }));

    await expect(fetchPendingInteractions(
      "session-1",
      new AbortController().signal,
    )).rejects.toMatchObject({ code: "interaction_unavailable" });

    expect(hangingSignal).not.toBeNull();
    expect((hangingSignal as unknown as AbortSignal).aborted).toBe(true);
  });

  it("bounds one pending request round to 25 seconds", async () => {
    vi.useFakeTimers();
    const requestSignals: AbortSignal[] = [];
    vi.stubGlobal("fetch", vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
      const requestSignal = init?.signal as AbortSignal;
      requestSignals.push(requestSignal);
      return new Promise<Response>((_resolve, reject) => {
        requestSignal.addEventListener("abort", () => {
          reject(new DOMException("aborted", "AbortError"));
        }, { once: true });
      });
    }));
    try {
      const request = fetchPendingInteractions("session-1", new AbortController().signal);
      const rejection = expect(request).rejects.toMatchObject({ code: "interaction_unavailable" });

      await vi.advanceTimersByTimeAsync(24_999);
      expect(requestSignals).toHaveLength(2);
      expect(requestSignals.every((signal) => !signal.aborted)).toBe(true);
      await vi.advanceTimersByTimeAsync(1);

      await rejection;
      expect(requestSignals.every((signal) => signal.aborted)).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });
});
