import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "../app/App";

describe("App shell", () => {
  beforeEach(() => {
    window.history.replaceState({}, "", "/");
  });

  it("marks unavailable features as disabled and non-functional", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ status: "ready" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );
    render(<App />);

    expect(screen.getByRole("button", { name: "添加附件，即将支持" })).toBeDisabled();
    expect(screen.getByRole("button", { name: /默认模型/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: /知识库管理/ })).toBeDisabled();
    expect(screen.getByText("会话 ID 不是登录身份。", { exact: false })).toBeVisible();
    await waitFor(() => expect(screen.getByText("服务可用")).toBeVisible());
  });

  it("stops an active stream and labels the turn as cancelled", async () => {
    const cancel = vi.fn();
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      if (String(input) === "/ready") {
        return Promise.resolve(
          new Response(JSON.stringify({ status: "ready" }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }
      return Promise.resolve(
        new Response(new ReadableStream<Uint8Array>({ cancel }), { status: 200 }),
      );
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    render(<App />);

    await user.type(screen.getByRole("textbox", { name: /向知识库提问/ }), "测试停止");
    await user.click(screen.getByRole("button", { name: "发送" }));
    await waitFor(() => expect(screen.getByRole("button", { name: /停止/ })).toBeVisible());
    await user.click(screen.getByRole("button", { name: /停止/ }));

    await waitFor(() => expect(cancel).toHaveBeenCalledOnce());
    expect(screen.getByText("已停止，本轮未保存为成功回答。")).toBeVisible();
  });

  it("cancels an active stream when the workspace unmounts", async () => {
    const cancel = vi.fn();
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        if (String(input) === "/ready") {
          return Promise.resolve(
            new Response(JSON.stringify({ status: "ready" }), {
              status: 200,
              headers: { "Content-Type": "application/json" },
            }),
          );
        }
        return Promise.resolve(
          new Response(new ReadableStream<Uint8Array>({ cancel }), { status: 200 }),
        );
      }),
    );
    const user = userEvent.setup();
    const view = render(<App />);

    await user.type(screen.getByRole("textbox", { name: /向知识库提问/ }), "测试卸载");
    await user.click(screen.getByRole("button", { name: "发送" }));
    await waitFor(() => expect(screen.getByRole("button", { name: /停止/ })).toBeVisible());
    view.unmount();

    await waitFor(() => expect(cancel).toHaveBeenCalledOnce());
  });

  it("blocks sends while reset is pending and rotates only after reset succeeds", async () => {
    localStorage.setItem("ragflow_session_id", "old-session");
    let finishReset: ((response: Response) => void) | undefined;
    const resetResponse = new Promise<Response>((resolve) => {
      finishReset = resolve;
    });
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/ready") {
        return Promise.resolve(
          new Response(JSON.stringify({ status: "ready" }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }
      if (url === "/session/reset?session_id=old-session") return resetResponse;
      return Promise.resolve(new Response("unexpected request", { status: 500 }));
    });
    vi.stubGlobal("fetch", fetchMock);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const user = userEvent.setup();
    render(<App />);

    const input = screen.getByRole("textbox", { name: /向知识库提问/ });
    await user.type(input, "重置期间不能发送");
    await user.click(screen.getByRole("button", { name: "新建对话" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      "/session/reset?session_id=old-session",
      expect.objectContaining({ method: "POST" }),
    ));

    expect(input).toHaveAttribute("readonly");
    fireEvent.submit(input.closest("form")!);
    expect(fetchMock.mock.calls.some(([request]) => String(request).startsWith("/query/stream"))).toBe(false);
    expect(localStorage.getItem("ragflow_session_id")).toBe("old-session");

    finishReset?.(new Response(null, { status: 200 }));
    await waitFor(() => expect(localStorage.getItem("ragflow_session_id")).not.toBe("old-session"));
    expect(screen.getByRole("heading", { name: "从知识库中找到可信答案" })).toBeVisible();
  });
});
