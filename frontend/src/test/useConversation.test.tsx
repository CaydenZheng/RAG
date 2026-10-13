import { act, render, renderHook, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { AgentInteraction, InteractionDecision } from "../api/types";
import { useConversation } from "../app/useConversation";
import { AgentInteractions } from "../components/AgentInteractions";

const RESPONSE_LOSS_CASES: ReadonlyArray<{
  pending: Record<string, unknown>;
  decision: InteractionDecision;
}> = [
  {
    pending: {
      id: "approval-1",
      tool_name: "send_message",
      source: "native",
      provider: null,
      category: "external",
      params: {},
    },
    decision: { kind: "approval", id: "approval-1", action: "approve" },
  },
  {
    pending: {
      id: "form-1",
      server_id: "interactive",
      mode: "form",
      message: "Input",
      requested_schema: { type: "object", properties: {} },
    },
    decision: { kind: "form", id: "form-1", action: "accept", content: {} },
  },
  {
    pending: {
      id: "url-1",
      server_id: "oauth-provider",
      mode: "url",
      message: "Login",
      url: "https://login.example.test/authorize?token=private",
      elicitation_id: "elicitation-1",
    },
    decision: { kind: "url", id: "url-1", action: "accept" },
  },
];

describe("useConversation interaction outcomes", () => {
  it.each(RESPONSE_LOSS_CASES)(
    "locks an unconfirmed $decision.kind submission as outcome unknown",
    async ({ pending, decision }) => {
      vi.useFakeTimers();
      let serverAccepted = false;
      vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
        const path = String(input);
        if (path === "/agent/chat/stream") {
          return Promise.resolve(new Response(new ReadableStream<Uint8Array>()));
        }
        if (init?.method === "POST") {
          serverAccepted = true;
          return Promise.reject(new TypeError("private transport detail"));
        }
        const matchesFamily = decision.kind === "approval"
          ? path.startsWith("/agent/approvals/")
          : path.startsWith("/agent/elicitation/");
        const interactions = matchesFamily && !serverAccepted ? [pending] : [];
        return Promise.resolve(new Response(JSON.stringify({ pending: interactions }), {
          headers: { "Content-Type": "application/json" },
        }));
      }));
      const conversation = renderHook(() => useConversation("agent"));
      try {
        await act(async () => {
          void conversation.result.current.send("Synthetic task");
          await vi.advanceTimersByTimeAsync(0);
        });
        expect(conversation.result.current.state.turns[0].interactions).toHaveLength(1);

        await act(async () => {
          await conversation.result.current.respondToInteraction(decision);
        });

        const interaction: AgentInteraction = conversation.result.current.state.turns[0].interactions[0];
        expect(serverAccepted).toBe(true);
        expect(interaction).toMatchObject({
          status: "outcome_unknown",
          error: "未收到提交确认，请核对工具执行结果后再继续。",
        });

        render(<AgentInteractions interactions={[interaction]} onDecision={vi.fn()} />);
        expect(screen.getByText("结果未知")).toBeVisible();
        for (const button of screen.getAllByRole("button")) expect(button).toBeDisabled();

        await act(async () => { await vi.advanceTimersByTimeAsync(350); });
        expect(conversation.result.current.state.turns[0].interactions[0].status).toBe("outcome_unknown");
      } finally {
        conversation.unmount();
        vi.useRealTimers();
      }
    },
  );
});
