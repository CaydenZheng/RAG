import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
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

  it("applies and persists an explicit theme without treating it as identity", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response(JSON.stringify({ status: "ready" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      })),
    );
    const user = userEvent.setup();
    render(<App />);

    await user.selectOptions(screen.getByRole("combobox", { name: "外观主题" }), "dark");

    expect(document.documentElement).toHaveAttribute("data-theme", "dark");
    expect(document.documentElement).toHaveAttribute("data-theme-preference", "dark");
    expect(localStorage.getItem("ragflow_theme")).toBe("dark");
  });

  it("follows system theme changes while the system preference is selected", async () => {
    let dark = true;
    let themeListener: (() => void) | undefined;
    vi.stubGlobal("matchMedia", vi.fn().mockImplementation((query: string) => ({
      get matches() { return query === "(prefers-color-scheme: dark)" ? dark : false; },
      addEventListener: (_type: string, nextListener: () => void) => {
        if (query === "(prefers-color-scheme: dark)") themeListener = nextListener;
      },
      removeEventListener: vi.fn(),
    })));
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response(JSON.stringify({ status: "ready" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      })),
    );
    render(<App />);

    expect(screen.getByRole("combobox", { name: "外观主题" })).toHaveValue("system");
    expect(document.documentElement).toHaveAttribute("data-theme", "dark");

    dark = false;
    act(() => themeListener?.());

    expect(document.documentElement).toHaveAttribute("data-theme", "light");
  });

  it("moves focus to the workspace after an in-app route change", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response(JSON.stringify({ status: "ready" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      })),
    );
    const user = userEvent.setup();
    render(<App />);

    await user.click(screen.getByRole("link", { name: "Agent" }));

    expect(screen.getByRole("main")).toHaveFocus();
    expect(screen.getByRole("heading", { name: "Agent 工作台" })).toBeVisible();
  });

  it("keeps the skip link first in the initial keyboard focus order", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response(JSON.stringify({ status: "ready" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      })),
    );
    const user = userEvent.setup();
    render(<App />);

    await user.tab();

    expect(screen.getByRole("link", { name: "跳到主要内容" })).toHaveFocus();
  });

  it("fills a suggested question without sending it automatically", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ status: "ready" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    render(<App />);

    await user.click(screen.getByRole("button", { name: "总结知识库中的核心观点" }));

    expect(screen.getByRole("textbox", { name: /向知识库提问/ })).toHaveValue(
      "总结知识库中的核心观点",
    );
    expect(fetchMock.mock.calls.some(([request]) => String(request).startsWith("/query/stream"))).toBe(false);
  });

  it("stops an active stream and labels the turn as cancelled", async () => {
    const cancel = vi.fn();
    const fetchMock = vi.fn((input: RequestInfo | URL, _init?: RequestInit) => {
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

  it("polls Approval alongside the Agent stream and shares its cancellation signal", async () => {
    window.history.replaceState({}, "", "/agent");
    const cancel = vi.fn();
    let approvalPollCount = 0;
    let activePollSignal: AbortSignal | null = null;
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/ready") {
        return Promise.resolve(new Response(JSON.stringify({ status: "ready" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }));
      }
      if (url === "/agent/chat/stream") {
        return Promise.resolve(new Response(new ReadableStream<Uint8Array>({ cancel }), { status: 200 }));
      }
      if (url.startsWith("/agent/approvals/")) {
        approvalPollCount += 1;
        if (approvalPollCount > 1) {
          activePollSignal = init?.signal as AbortSignal;
          return new Promise<Response>((_resolve, reject) => {
            activePollSignal?.addEventListener("abort", () => {
              reject(new DOMException("aborted", "AbortError"));
            }, { once: true });
          });
        }
        return Promise.resolve(new Response(JSON.stringify({ pending: [{
          id: "approval-1",
          tool_name: "send_message",
          source: "native",
          provider: "local-provider",
          category: "external",
          params: { recipient: "private" },
          security_level: "critical",
        }] }), { status: 200, headers: { "Content-Type": "application/json" } }));
      }
      if (url.startsWith("/agent/elicitation/")) {
        return Promise.resolve(new Response(JSON.stringify({ pending: [] }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }));
      }
      return Promise.resolve(new Response(null, { status: 500 }));
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    render(<App />);

    await user.type(screen.getByRole("textbox", { name: /向 Agent 发送任务/ }), "发送一条消息");
    await user.click(screen.getByRole("button", { name: "发送" }));

    expect(await screen.findByRole("button", { name: "允许一次" })).toBeVisible();
    expect(screen.getByText("send_message")).toBeVisible();
    expect(screen.queryByText(/安全等级|critical/)).not.toBeInTheDocument();

    const streamCall = fetchMock.mock.calls.find(([request]) => String(request) === "/agent/chat/stream");
    const streamSignal = (streamCall?.[1] as RequestInit | undefined)?.signal;
    expect(streamSignal?.aborted).toBe(false);
    await waitFor(() => expect(activePollSignal).not.toBeNull());
    expect((activePollSignal as unknown as AbortSignal).aborted).toBe(false);

    await user.click(screen.getByRole("button", { name: /停止/ }));
    expect(streamSignal?.aborted).toBe(true);
    expect((activePollSignal as unknown as AbortSignal).aborted).toBe(true);
    await waitFor(() => expect(cancel).toHaveBeenCalledOnce());
  });

  it("preserves separate drafts while switching routes without reloading", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ status: "ready" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    ));
    const user = userEvent.setup();
    render(<App />);

    const ragInput = screen.getByRole("textbox", { name: /向知识库提问/ });
    await user.type(ragInput, "RAG 草稿");
    await user.click(screen.getByRole("link", { name: "Agent" }));

    expect(window.location.pathname).toBe("/agent");
    const agentInput = screen.getByRole("textbox", { name: /向 Agent 发送任务/ });
    expect(agentInput).toHaveValue("");
    await user.type(agentInput, "Agent 草稿");
    await user.click(screen.getByRole("link", { name: "知识问答" }));

    expect(window.location.pathname).toBe("/");
    expect(screen.getByRole("textbox", { name: /向知识库提问/ })).toHaveValue("RAG 草稿");
  });

  it("auto-follows streamed chunks only near the bottom and resumes on request", async () => {
    let streamController: ReadableStreamDefaultController<Uint8Array> | undefined;
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      if (String(input) === "/ready") {
        return Promise.resolve(new Response(JSON.stringify({ status: "ready" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }));
      }
      return Promise.resolve(new Response(new ReadableStream<Uint8Array>({
        start(controller) { streamController = controller; },
      }), { status: 200 }));
    }));
    const user = userEvent.setup();
    render(<App />);
    const region = screen.getByRole("region", { name: "对话内容" });
    Object.defineProperties(region, {
      scrollHeight: { configurable: true, value: 1000 },
      clientHeight: { configurable: true, value: 400 },
      scrollTop: { configurable: true, value: 580, writable: true },
    });
    const scrollTo = vi.fn();
    Object.defineProperty(region, "scrollTo", { configurable: true, value: scrollTo });

    fireEvent.scroll(region);
    await user.type(screen.getByRole("textbox", { name: /向知识库提问/ }), "自动滚动");
    await user.click(screen.getByRole("button", { name: "发送" }));
    await waitFor(() => expect(scrollTo).toHaveBeenCalled());

    scrollTo.mockClear();
    region.scrollTop = 100;
    fireEvent.scroll(region);
    await act(async () => {
      streamController?.enqueue(new TextEncoder().encode('data: {"event":"chunk","done":false,"chunk":"一"}\n\n'));
      await Promise.resolve();
    });
    expect(scrollTo).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "回到最新" }));
    expect(scrollTo).toHaveBeenCalledWith({ top: 1000, behavior: "auto" });
    expect(screen.queryByRole("button", { name: "回到最新" })).not.toBeInTheDocument();
    scrollTo.mockClear();
    await act(async () => {
      streamController?.enqueue(new TextEncoder().encode('data: {"event":"chunk","done":false,"chunk":"二"}\n\n'));
      await Promise.resolve();
    });
    expect(scrollTo).toHaveBeenCalledWith({ top: 1000, behavior: "auto" });
  });

  it("cancels the active run on route switch and keeps its cancelled timeline isolated", async () => {
    window.history.replaceState({}, "", "/agent");
    const cancel = vi.fn();
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/ready") {
        return Promise.resolve(new Response(JSON.stringify({ status: "ready" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }));
      }
      if (url === "/agent/chat/stream") {
        return Promise.resolve(new Response(new ReadableStream<Uint8Array>({ cancel }), { status: 200 }));
      }
      if (url.startsWith("/agent/approvals/") || url.startsWith("/agent/elicitation/")) {
        return Promise.resolve(new Response(JSON.stringify({ pending: [] }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }));
      }
      return Promise.resolve(new Response(null, { status: 500 }));
    }));
    const user = userEvent.setup();
    render(<App />);

    await user.type(screen.getByRole("textbox", { name: /向 Agent 发送任务/ }), "不会串到 RAG");
    await user.click(screen.getByRole("button", { name: "发送" }));
    await user.click(screen.getByRole("link", { name: "知识问答" }));

    await waitFor(() => expect(cancel).toHaveBeenCalledOnce());
    expect(screen.queryByText("不会串到 RAG")).not.toBeInTheDocument();
    await user.click(screen.getByRole("link", { name: "Agent" }));
    expect(screen.getByText("不会串到 RAG")).toBeVisible();
    expect(screen.getByText("已停止，本轮未保存为成功回答。")).toBeVisible();
  });

  it("does not infer a backend timeout while interactions remain pending", async () => {
    vi.useFakeTimers();
    window.history.replaceState({}, "", "/agent");
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/ready") {
        return Promise.resolve(new Response(JSON.stringify({ status: "ready" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }));
      }
      if (url === "/agent/chat/stream") {
        return Promise.resolve(new Response(new ReadableStream<Uint8Array>(), { status: 200 }));
      }
      if (url.startsWith("/agent/approvals/")) {
        return Promise.resolve(new Response(JSON.stringify({ pending: [{
          id: "approval-timeout",
          tool_name: "slow_tool",
          source: "native",
          provider: null,
          category: "external",
          params: {},
        }] }), { status: 200, headers: { "Content-Type": "application/json" } }));
      }
      if (url.startsWith("/agent/elicitation/")) {
        return Promise.resolve(new Response(JSON.stringify({ pending: [
          {
            id: "form-timeout",
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
            id: "url-timeout",
            server_id: "oauth-provider",
            mode: "url",
            message: "Complete sign-in",
            url: "https://login.example.test/authorize?token=private",
            elicitation_id: "opaque-id",
          },
        ] }), { status: 200, headers: { "Content-Type": "application/json" } }));
      }
      return Promise.resolve(new Response(JSON.stringify({ pending: [] }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }));
    }));
    const view = render(<App />);
    try {
      const input = screen.getByRole("textbox", { name: /向 Agent 发送任务/ });
      fireEvent.change(input, { target: { value: "等待审批" } });
      fireEvent.submit(input.closest("form")!);
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });
      expect(screen.getByRole("button", { name: "允许一次" })).toBeEnabled();
      expect(screen.getByRole("button", { name: "提交并继续" })).toBeEnabled();
      expect(screen.getByRole("button", { name: "已完成，继续" })).toBeEnabled();

      await act(async () => { vi.advanceTimersByTime(25_000); });

      expect(screen.getAllByText("等待处理")).toHaveLength(3);
      expect(screen.getByRole("button", { name: "允许一次" })).toBeEnabled();
      expect(screen.getByRole("button", { name: "提交并继续" })).toBeEnabled();
      expect(screen.getByRole("button", { name: "已完成，继续" })).toBeEnabled();
    } finally {
      view.unmount();
      vi.useRealTimers();
    }
  });

  it("keeps an Approval submitting until the backend confirms it before 25 seconds", async () => {
    vi.useFakeTimers();
    window.history.replaceState({}, "", "/agent");
    let finishApproval: ((response: Response) => void) | undefined;
    const approvalResponse = new Promise<Response>((resolve) => {
      finishApproval = resolve;
    });
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/ready") {
        return Promise.resolve(new Response(JSON.stringify({ status: "ready" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }));
      }
      if (url === "/agent/chat/stream") {
        return Promise.resolve(new Response(new ReadableStream<Uint8Array>(), { status: 200 }));
      }
      if (url.startsWith("/agent/approvals/") && init?.method === "POST") {
        return approvalResponse;
      }
      if (url.startsWith("/agent/approvals/")) {
        return Promise.resolve(new Response(JSON.stringify({ pending: [{
          id: "approval-slow-submit",
          tool_name: "send_message",
          source: "native",
          provider: null,
          category: "external",
          params: {},
        }] }), { status: 200, headers: { "Content-Type": "application/json" } }));
      }
      return Promise.resolve(new Response(JSON.stringify({ pending: [] }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }));
    }));
    const view = render(<App />);
    try {
      const input = screen.getByRole("textbox", { name: /向 Agent 发送任务/ });
      fireEvent.change(input, { target: { value: "等待慢审批响应" } });
      fireEvent.submit(input.closest("form")!);
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });
      fireEvent.click(screen.getByRole("button", { name: "允许一次" }));
      expect(screen.getByText("正在提交")).toBeVisible();

      await act(async () => { vi.advanceTimersByTime(24_999); });
      expect(screen.getByText("正在提交")).toBeVisible();

      finishApproval?.(new Response(null, { status: 202 }));
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });
      expect(screen.getByText("已允许")).toBeVisible();
    } finally {
      view.unmount();
      vi.useRealTimers();
    }
  });

  it("reports an unknown Approval outcome when its POST is unconfirmed after 25 seconds", async () => {
    vi.useFakeTimers();
    window.history.replaceState({}, "", "/agent");
    let postSignal: AbortSignal | null = null;
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/ready") {
        return Promise.resolve(new Response(JSON.stringify({ status: "ready" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }));
      }
      if (url === "/agent/chat/stream") {
        return Promise.resolve(new Response(new ReadableStream<Uint8Array>(), { status: 200 }));
      }
      if (url.startsWith("/agent/approvals/") && init?.method === "POST") {
        postSignal = init.signal as AbortSignal;
        return new Promise<Response>((_resolve, reject) => {
          postSignal?.addEventListener("abort", () => {
            reject(new DOMException("aborted", "AbortError"));
          }, { once: true });
        });
      }
      if (url.startsWith("/agent/approvals/")) {
        return Promise.resolve(new Response(JSON.stringify({ pending: [{
          id: "approval-post-timeout",
          tool_name: "send_message",
          source: "native",
          provider: null,
          category: "external",
          params: {},
        }] }), { status: 200, headers: { "Content-Type": "application/json" } }));
      }
      return Promise.resolve(new Response(JSON.stringify({ pending: [] }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }));
    }));
    const view = render(<App />);
    try {
      const input = screen.getByRole("textbox", { name: /向 Agent 发送任务/ });
      fireEvent.change(input, { target: { value: "等待审批提交确认" } });
      fireEvent.submit(input.closest("form")!);
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });
      fireEvent.click(screen.getByRole("button", { name: "允许一次" }));
      expect(screen.getByText("正在提交")).toBeVisible();

      await act(async () => { await vi.advanceTimersByTimeAsync(25_000); });

      expect(screen.getByText("结果未知")).toBeVisible();
      expect(screen.getByRole("alert")).toHaveTextContent("未收到提交确认，请核对工具执行结果后再继续。");
      expect(postSignal).not.toBeNull();
      expect((postSignal as unknown as AbortSignal).aborted).toBe(true);
    } finally {
      view.unmount();
      vi.useRealTimers();
    }
  });

  it("reports an unknown Approval outcome when Agent DONE wins the submit race", async () => {
    window.history.replaceState({}, "", "/agent");
    let streamController: ReadableStreamDefaultController<Uint8Array> | undefined;
    let postSignal: AbortSignal | null = null;
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/ready") {
        return Promise.resolve(new Response(JSON.stringify({ status: "ready" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }));
      }
      if (url === "/agent/chat/stream") {
        return Promise.resolve(new Response(new ReadableStream<Uint8Array>({
          start(controller) { streamController = controller; },
        }), { status: 200 }));
      }
      if (url.startsWith("/agent/approvals/") && init?.method === "POST") {
        postSignal = init.signal as AbortSignal;
        return new Promise<Response>(() => undefined);
      }
      if (url.startsWith("/agent/approvals/")) {
        return Promise.resolve(new Response(JSON.stringify({ pending: [{
          id: "approval-done-race",
          tool_name: "send_message",
          source: "native",
          provider: null,
          category: "external",
          params: {},
        }] }), { status: 200, headers: { "Content-Type": "application/json" } }));
      }
      return Promise.resolve(new Response(JSON.stringify({ pending: [] }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }));
    }));
    const user = userEvent.setup();
    render(<App />);

    await user.type(screen.getByRole("textbox", { name: /向 Agent 发送任务/ }), "审批与 DONE 竞争");
    await user.click(screen.getByRole("button", { name: "发送" }));
    await user.click(await screen.findByRole("button", { name: "允许一次" }));
    expect(screen.getByText("正在提交")).toBeVisible();

    await act(async () => {
      streamController?.enqueue(new TextEncoder().encode(
        'data: {"done":true,"answer":"","iterations":1,"latency_ms":1}\n\n',
      ));
      await Promise.resolve();
    });

    expect(await screen.findByText("结果未知")).toBeVisible();
    expect(postSignal).not.toBeNull();
    expect((postSignal as unknown as AbortSignal).aborted).toBe(true);
  });

  it("aborts an in-flight Approval response together with the Agent run", async () => {
    window.history.replaceState({}, "", "/agent");
    let postSignal: AbortSignal | null = null;
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/ready") {
        return Promise.resolve(new Response(JSON.stringify({ status: "ready" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }));
      }
      if (url === "/agent/chat/stream") {
        return Promise.resolve(new Response(new ReadableStream<Uint8Array>(), { status: 200 }));
      }
      if (url.startsWith("/agent/approvals/") && init?.method === "POST") {
        postSignal = init.signal as AbortSignal;
        return new Promise<Response>(() => undefined);
      }
      if (url.startsWith("/agent/approvals/")) {
        return Promise.resolve(new Response(JSON.stringify({ pending: [{
          id: "approval-submit",
          tool_name: "send_message",
          source: "native",
          provider: null,
          category: "external",
          params: {},
        }] }), { status: 200, headers: { "Content-Type": "application/json" } }));
      }
      return Promise.resolve(new Response(JSON.stringify({ pending: [] }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }));
    }));
    const user = userEvent.setup();
    render(<App />);

    await user.type(screen.getByRole("textbox", { name: /向 Agent 发送任务/ }), "需要审批");
    await user.click(screen.getByRole("button", { name: "发送" }));
    await user.click(await screen.findByRole("button", { name: "允许一次" }));
    expect(await screen.findByText("正在提交")).toBeVisible();
    await user.click(screen.getByRole("button", { name: /停止/ }));

    expect(postSignal).not.toBeNull();
    expect((postSignal as unknown as AbortSignal).aborted).toBe(true);
    expect(screen.getByText("结果未知")).toBeVisible();
  });
});
