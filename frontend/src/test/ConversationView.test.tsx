import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { ConversationState } from "../app/conversationState";
import { ConversationView } from "../components/ConversationView";

const completedState: ConversationState = {
  status: "completed",
  nextTurnId: 2,
  turns: [
    {
      id: 1,
      status: "completed",
      question: "问题",
      answer: "**完成回答**",
      error: "",
      sources: [],
      warnings: [],
      steps: [],
      latencyMs: 10,
      indexVersion: "v1",
      iterations: null,
      interactions: [],
    },
  ],
};

const emptyState: ConversationState = {
  status: "idle",
  nextTurnId: 1,
  turns: [],
};

function completedStateFor(question: string, answer: string): ConversationState {
  return {
    ...completedState,
    turns: [{ ...completedState.turns[0], question, answer }],
  };
}

describe("ConversationView", () => {
  it("copies a completed answer and announces success without moving focus", async () => {
    const user = userEvent.setup();
    const writeText = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue(undefined);
    render(<ConversationView mode="rag" state={completedState} onInteractionDecision={vi.fn()} />);

    const copyButton = screen.getByRole("button", { name: "复制回答" });
    copyButton.focus();
    await user.click(copyButton);

    expect(writeText).toHaveBeenCalledWith("**完成回答**");
    expect(screen.getByRole("status")).toHaveTextContent("回答已复制");
    expect(copyButton).toHaveFocus();
  });

  it("does not carry copy feedback to the same turn ID in another mode", async () => {
    const user = userEvent.setup();
    vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue(undefined);
    const view = render(
      <ConversationView mode="rag" state={completedState} onInteractionDecision={vi.fn()} />,
    );

    await user.click(screen.getByRole("button", { name: "复制回答" }));
    expect(screen.getByRole("status")).toHaveTextContent("回答已复制");

    view.rerender(
      <ConversationView mode="agent" state={completedState} onInteractionDecision={vi.fn()} />,
    );

    expect(screen.getByRole("status")).toBeEmptyDOMElement();
  });

  it("does not carry completed copy feedback across a same-mode session reset", async () => {
    const user = userEvent.setup();
    vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue(undefined);
    const view = render(
      <ConversationView
        mode="rag"
        state={completedStateFor("旧问题", "旧回答")}
        onInteractionDecision={vi.fn()}
      />,
    );

    await user.click(screen.getByRole("button", { name: "复制回答" }));
    expect(screen.getByRole("status")).toHaveTextContent("回答已复制");

    view.rerender(
      <ConversationView mode="rag" state={emptyState} onInteractionDecision={vi.fn()} />,
    );
    view.rerender(
      <ConversationView
        mode="rag"
        state={completedStateFor("新问题", "新回答")}
        onInteractionDecision={vi.fn()}
      />,
    );

    expect(screen.getByRole("status")).toBeEmptyDOMElement();
  });

  it("ignores a copy result that settles after a same-mode session reset", async () => {
    const user = userEvent.setup();
    let finishWrite: (() => void) | null = null;
    const writeResult = new Promise<void>((resolve) => { finishWrite = resolve; });
    vi.spyOn(navigator.clipboard, "writeText").mockReturnValue(writeResult);
    const view = render(
      <ConversationView
        mode="rag"
        state={completedStateFor("旧问题", "旧回答")}
        onInteractionDecision={vi.fn()}
      />,
    );

    await user.click(screen.getByRole("button", { name: "复制回答" }));
    view.rerender(
      <ConversationView mode="rag" state={emptyState} onInteractionDecision={vi.fn()} />,
    );
    view.rerender(
      <ConversationView
        mode="rag"
        state={completedStateFor("新问题", "新回答")}
        onInteractionDecision={vi.fn()}
      />,
    );
    await act(async () => {
      finishWrite?.();
      await writeResult;
    });

    expect(screen.getByRole("status")).toBeEmptyDOMElement();
  });

  it("links only HTTP(S) citation sources", () => {
    const state: ConversationState = {
      ...completedState,
      turns: [{
        ...completedState.turns[0],
        sources: [
          { ref: "1", source: "https://docs.example.test/page", score: 0.9, text: "safe" },
          { ref: "2", source: "javascript:alert(1)", score: 0.8, text: "unsafe" },
        ],
        warnings: ["rerank_timeout"],
      }],
    };
    render(<ConversationView mode="rag" state={state} onInteractionDecision={vi.fn()} />);

    expect(screen.getByRole("link", { name: "https://docs.example.test/page" })).toHaveAttribute(
      "href",
      "https://docs.example.test/page",
    );
    expect(screen.getByText("javascript:alert(1)").closest("a")).toBeNull();
    expect(screen.getByText("精排处理超时，当前结果使用融合排序。")).toBeVisible();
    expect(screen.getByText("引用来源 · 2")).toBeVisible();
  });

  it("keeps Agent tool progress collapsible and shows only the approved result summary", async () => {
    const user = userEvent.setup();
    const state: ConversationState = {
      status: "completed",
      nextTurnId: 2,
      turns: [{
        ...completedState.turns[0],
        answer: "2",
        steps: [
          { id: "plan", kind: "planning", iteration: 1, tool: "", params: null, success: null },
          { id: "call", kind: "tool_call", iteration: 1, tool: "calculator", params: { expression: "1+1" }, success: null },
          { id: "done", kind: "tool_done", iteration: 1, tool: "calculator", params: null, success: true, summary:"计算结果：2" },
        ],
      }],
    };
    render(<ConversationView mode="agent" state={state} onInteractionDecision={vi.fn()} />);

    const process = screen.getByText("执行过程 · 3 个步骤").closest("details");
    expect(process).not.toHaveAttribute("open");
    await user.click(screen.getByText("执行过程 · 3 个步骤"));
    expect(screen.getByText("计算结果：2")).toBeVisible();
    expect(screen.queryByText(/"expression"/)).not.toBeInTheDocument();
  });
});
