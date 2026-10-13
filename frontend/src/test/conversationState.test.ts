import { describe, expect, it } from "vitest";

import { conversationReducer, initialConversationState } from "../app/conversationState";
import type { AgentInteraction } from "../api/types";

describe("conversation reducer terminals", () => {
  it("keeps a completed turn when the next turn starts", () => {
    const firstStarted = conversationReducer(initialConversationState, {
      type: "start",
      question: "第一个问题",
    });
    const firstCompleted = conversationReducer(firstStarted, {
      type: "complete_rag",
      completion: {
        answer: "第一个回答",
        sources: [],
        warnings: [],
        latencyMs: 1,
        indexVersion: "v1",
      },
    });
    const secondStarted = conversationReducer(firstCompleted, {
      type: "start",
      question: "第二个问题",
    });

    expect(secondStarted.turns).toMatchObject([
      { question: "第一个问题", answer: "第一个回答", status: "completed" },
      { question: "第二个问题", answer: "", status: "connecting" },
    ]);
  });

  it("does not let a late DONE replace cancellation", () => {
    const started = conversationReducer(initialConversationState, {
      type: "start",
      question: "问题",
    });
    const cancelled = conversationReducer(started, { type: "cancel" });
    const lateDone = conversationReducer(cancelled, {
      type: "complete_rag",
      completion: {
        answer: "迟到回答",
        sources: [],
        warnings: [],
        latencyMs: 1,
        indexVersion: "v1",
      },
    });
    expect(lateDone).toEqual(cancelled);
  });

  it("does not let a late ERROR replace success", () => {
    const started = conversationReducer(initialConversationState, {
      type: "start",
      question: "问题",
    });
    const completed = conversationReducer(started, {
      type: "complete_agent",
      completion: { answer: "完成", iterations: 1, latencyMs: 1 },
    });
    const lateError = conversationReducer(completed, { type: "fail", message: "迟到错误" });
    expect(lateError).toEqual(completed);
  });

  it("accepts only one terminal result when an unknown outcome and response compete", () => {
    const started = conversationReducer(initialConversationState, { type: "start", question: "问题" });
    const pending = conversationReducer(started, {
      type: "sync_interactions",
      interactions: [{
        kind: "approval",
        id: "approval-1",
        status: "pending",
        error: "",
        toolName: "send_message",
        source: "native",
        provider: null,
        category: "external",
        params: {},
      }],
    });
    expect(pending.status).toBe("waiting_user");
    const outcomeUnknown = conversationReducer(pending, {
      type: "interaction_status",
      kind: "approval",
      id: "approval-1",
      status: "outcome_unknown",
    });
    const lateApproval = conversationReducer(outcomeUnknown, {
      type: "interaction_status",
      kind: "approval",
      id: "approval-1",
      status: "approved",
    });

    expect(lateApproval.turns[0].interactions[0].status).toBe("outcome_unknown");
    expect(lateApproval.status).toBe("streaming");
  });

  it("closes a vanished pending interaction without accepting a late response", () => {
    const started = conversationReducer(initialConversationState, { type: "start", question: "问题" });
    const pending = conversationReducer(started, {
      type: "sync_interactions",
      interactions: [{
        kind: "approval",
        id: "approval-closed",
        status: "pending",
        error: "",
        toolName: "send_message",
        source: "native",
        provider: null,
        category: "external",
        params: {},
      }],
    });
    const closed = conversationReducer(pending, { type: "sync_interactions", interactions: [] });
    const lateResponse = conversationReducer(closed, {
      type: "interaction_status",
      kind: "approval",
      id: "approval-closed",
      status: "approved",
    });

    expect(closed.turns[0].interactions[0].status).toBe("closed");
    expect(lateResponse).toEqual(closed);
  });

  it("gives form and URL Elicitation one terminal across unknown outcome, close, and late responses", () => {
    const interactions: AgentInteraction[] = [
      {
        kind: "form",
        id: "form-1",
        serverId: "interactive",
        status: "pending",
        error: "",
        message: "Choose an answer",
        requestedSchema: { type: "object", properties: {} },
      },
      {
        kind: "url",
        id: "url-1",
        serverId: "oauth-provider",
        status: "pending",
        error: "",
        message: "Complete sign-in",
        url: "https://login.example.test/authorize?token=private",
        elicitationId: "opaque-id",
      },
    ];
    const started = conversationReducer(initialConversationState, { type: "start", question: "问题" });
    const pending = conversationReducer(started, { type: "sync_interactions", interactions });
    const formOutcomeUnknown = conversationReducer(pending, {
      type: "interaction_status",
      kind: "form",
      id: "form-1",
      status: "outcome_unknown",
    });
    const urlClosed = conversationReducer(formOutcomeUnknown, {
      type: "sync_interactions",
      interactions: [],
    });
    const lateForm = conversationReducer(urlClosed, {
      type: "interaction_status",
      kind: "form",
      id: "form-1",
      status: "accepted",
    });
    const lateUrl = conversationReducer(lateForm, {
      type: "interaction_status",
      kind: "url",
      id: "url-1",
      status: "accepted",
    });

    expect(urlClosed.turns[0].interactions.map((interaction) => interaction.status)).toEqual([
      "outcome_unknown",
      "closed",
    ]);
    expect(lateUrl).toEqual(urlClosed);
  });
});
