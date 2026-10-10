import { describe, expect, it } from "vitest";

import { conversationReducer, initialConversationState } from "../app/conversationState";

describe("conversation reducer terminals", () => {
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
});
