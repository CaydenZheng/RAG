import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { ApprovalInteraction, FormElicitationInteraction, UrlElicitationInteraction } from "../api/types";
import { AgentInteractions } from "../components/AgentInteractions";

describe("Agent Elicitation", () => {
  it("maps every Approval decision to the documented action", async () => {
    const interaction: ApprovalInteraction = {
      kind: "approval",
      id: "approval-1",
      status: "pending",
      error: "",
      toolName: "send_message",
      source: "native",
      provider: null,
      category: "external",
      params: { recipient: "private" },
    };
    const onDecision = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    render(<AgentInteractions interactions={[interaction]} onDecision={onDecision} />);

    await user.click(screen.getByRole("button", { name: "允许一次" }));
    await user.click(screen.getByRole("button", { name: "拒绝" }));
    await user.click(screen.getByRole("button", { name: "取消任务" }));

    expect(onDecision.mock.calls.map(([decision]) => decision.action)).toEqual([
      "approve",
      "reject",
      "cancel",
    ]);
    expect(screen.queryByText(/安全等级/)).not.toBeInTheDocument();
  });

  it("validates a supported form inline and submits typed content", async () => {
    const interaction: FormElicitationInteraction = {
      kind: "form",
      id: "form-1",
      serverId: "interactive",
      status: "pending",
      error: "",
      message: "Choose an answer",
      requestedSchema: {
        type: "object",
        properties: {
          answer: { title: "Answer", type: "string" },
          count: { title: "Count", type: "integer" },
        },
        required: ["answer", "count"],
      },
    };
    const onDecision = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    render(<AgentInteractions interactions={[interaction]} onDecision={onDecision} />);

    await user.click(screen.getByRole("button", { name: "提交并继续" }));
    expect(screen.getAllByText("此项为必填项。")).toHaveLength(2);
    expect(screen.getByText("请检查表单中标出的字段。")).toHaveFocus();
    expect(onDecision).not.toHaveBeenCalled();

    await user.type(screen.getByRole("textbox", { name: /Answer/ }), "yes");
    await user.type(screen.getByRole("spinbutton", { name: /Count/ }), "2");
    await user.click(screen.getByRole("button", { name: "提交并继续" }));

    expect(onDecision).toHaveBeenCalledWith({
      kind: "form",
      id: "form-1",
      action: "accept",
      content: { answer: "yes", count: 2 },
    });
    await user.click(screen.getByRole("button", { name: "拒绝" }));
    await user.click(screen.getByRole("button", { name: "取消任务" }));
    expect(onDecision).toHaveBeenCalledWith({ kind: "form", id: "form-1", action: "decline" });
    expect(onDecision).toHaveBeenCalledWith({ kind: "form", id: "form-1", action: "cancel" });
  });

  it("keeps URL Elicitation separate and opens the full URL only after a click", async () => {
    const interaction: UrlElicitationInteraction = {
      kind: "url",
      id: "url-1",
      serverId: "oauth-provider",
      status: "pending",
      error: "",
      message: "Complete sign-in at https://login.example.test/authorize?token=message-secret",
      url: "https://login.example.test/authorize?token=private-query",
      elicitationId: "opaque-id",
    };
    const open = vi.spyOn(window, "open").mockImplementation(() => null);
    const onDecision = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    render(<AgentInteractions interactions={[interaction]} onDecision={onDecision} />);

    expect(open).not.toHaveBeenCalled();
    expect(screen.getByText("login.example.test")).toBeVisible();
    expect(screen.queryByText(/private-query/)).not.toBeInTheDocument();
    expect(screen.queryByText(/message-secret/)).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "打开安全页面" }));

    expect(open).toHaveBeenCalledWith(
      "https://login.example.test/authorize?token=private-query",
      "_blank",
      "noopener,noreferrer",
    );
    await user.click(screen.getByRole("button", { name: "已完成，继续" }));
    await user.click(screen.getByRole("button", { name: "拒绝" }));
    await user.click(screen.getByRole("button", { name: "取消任务" }));
    expect(onDecision.mock.calls.map(([decision]) => decision.action)).toEqual([
      "accept",
      "decline",
      "cancel",
    ]);
  });
});
