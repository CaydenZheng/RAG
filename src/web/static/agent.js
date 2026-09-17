(() => {
  "use strict";

  const SESSION_KEY = "ragflow_agent_session";
  const agentForm = document.getElementById("agentForm");
  const messageInput = document.getElementById("msgInput");
  const sendBtn = document.getElementById("sendBtn");
  const stopBtn = document.getElementById("stopBtn");
  const newSessionBtn = document.getElementById("newSessionBtn");
  const loading = document.getElementById("loading");
  const error = document.getElementById("error");
  const chatArea = document.getElementById("chatArea");
  const processPanel = document.getElementById("processPanel");
  const thinking = document.getElementById("thinking");
  const answerEl = document.getElementById("answer");
  const answerCard = document.getElementById("answerCard");
  const latencyEl = document.getElementById("latency");
  const iterationEl = document.getElementById("iterCount");
  let sessionId = localStorage.getItem(SESSION_KEY);
  let activeRequestController = null;
  let activeAnimationFrameId = null;
  let toolDetailCounter = 0;

  if (!sessionId) {
    sessionId = crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).slice(2, 14);
    localStorage.setItem(SESSION_KEY, sessionId);
  }

  function setRunning(running) {
    agentForm.setAttribute("aria-busy", String(running));
    chatArea.setAttribute("aria-busy", String(running));
    messageInput.readOnly = running;
    sendBtn.classList.toggle("hidden", running);
    stopBtn.classList.toggle("active", running);
    loading.classList.toggle("active", running);
  }

  function showError(message) {
    error.textContent = String(message);
    error.classList.add("active");
  }

  function clearError() {
    error.textContent = "";
    error.classList.remove("active");
  }

  function createStep(state = "neutral") {
    const step = SafeRender.createElement("div", "step");
    step.setAttribute("role", "listitem");
    step.dataset.state = state;
    const index = SafeRender.createElement(
      "span",
      "step-index",
      String(thinking.children.length + 1).padStart(2, "0"),
    );
    index.setAttribute("aria-hidden", "true");
    const content = SafeRender.createElement("div", "step-content");
    step.append(index, content);
    return { step, content };
  }

  function appendStep(step) {
    processPanel.classList.remove("hidden");
    thinking.appendChild(step);
  }

  function stopAgent(showMessage = true) {
    if (activeRequestController) {
      activeRequestController.abort();
      activeRequestController = null;
    }
    if (activeAnimationFrameId !== null) {
      cancelAnimationFrame(activeAnimationFrameId);
      activeAnimationFrameId = null;
    }

    setRunning(false);
    answerEl.classList.remove("streaming");

    if (showMessage) {
      answerCard.classList.remove("hidden");
      if (!answerEl.textContent) answerEl.textContent = "运行已停止。";
      latencyEl.textContent = "—";
    }
  }

  async function send() {
    const message = messageInput.value.trim();
    if (!message) {
      messageInput.focus();
      return;
    }

    stopAgent(false);
    clearError();
    setRunning(true);
    thinking.replaceChildren();
    processPanel.classList.add("hidden");
    answerEl.textContent = "";
    answerEl.classList.add("streaming");
    answerCard.classList.add("hidden");
    latencyEl.textContent = "—";
    iterationEl.textContent = "—";
    toolDetailCounter = 0;

    const controller = new AbortController();
    activeRequestController = controller;

    const typingInterval = 35;
    const chunkQueue = [];
    let fullAnswer = "";
    let animating = false;
    let finished = false;
    let completionData = null;
    let lastRenderTime = 0;
    let firstRender = true;
    const startedAt = Date.now();
    let iterationCount = 0;

    controller.signal.addEventListener(
      "abort",
      () => {
        chunkQueue.length = 0;
        if (activeAnimationFrameId !== null) {
          cancelAnimationFrame(activeAnimationFrameId);
          activeAnimationFrameId = null;
        }
        animating = false;
      },
      { once: true },
    );

    function finishRendering() {
      if (controller.signal.aborted || activeRequestController !== controller) return;

      answerEl.classList.remove("streaming");
      SafeRender.appendLinkifiedText(answerEl, fullAnswer);
      activeRequestController = null;
      activeAnimationFrameId = null;
      setRunning(false);
      answerCard.classList.remove("hidden");

      const serverLatency = Number(completionData?.latency_ms);
      const latency = Number.isFinite(serverLatency) ? serverLatency : Date.now() - startedAt;
      latencyEl.textContent = Math.round(latency) + " ms";
      iterationEl.textContent = String(iterationCount);
    }

    function renderFrame(timestamp) {
      activeAnimationFrameId = null;
      if (controller.signal.aborted || activeRequestController !== controller) return;

      if (chunkQueue.length === 0) {
        animating = false;
        if (finished) finishRendering();
        return;
      }

      if (firstRender || timestamp - lastRenderTime >= typingInterval) {
        fullAnswer += chunkQueue.shift();
        answerEl.textContent = fullAnswer;
        lastRenderTime = timestamp;
        firstRender = false;
      }
      activeAnimationFrameId = requestAnimationFrame(renderFrame);
    }

    function handleEvent(data) {
      if (controller.signal.aborted || activeRequestController !== controller) return;

      if (data.error) {
        const errorMessage = typeof data.error === "object"
          ? data.error.message
          : data.error;
        answerEl.classList.remove("streaming");
        setRunning(false);
        showError(errorMessage || "Agent 处理失败，请稍后重试。");
        controller.abort();
        if (activeRequestController === controller) activeRequestController = null;
        return;
      }

      if (data.step === "planning") {
        loading.classList.remove("active");
        iterationCount = data.iteration;
        const { step, content } = createStep();
        content.textContent = "第 " + String(data.iteration) + " 轮规划";
        appendStep(step);
        return;
      }

      if (data.step === "tool_call") {
        const { step, content } = createStep();
        const toolName = SafeRender.createElement("b", "", data.tool ?? "未知工具");
        const detailId = "toolDetail" + String(++toolDetailCounter);
        const toggle = SafeRender.createElement("button", "step-toggle", "查看参数");
        toggle.type = "button";
        toggle.setAttribute("aria-controls", detailId);
        toggle.setAttribute("aria-expanded", "false");

        const detail = SafeRender.createElement(
          "pre",
          "step-detail hidden",
          JSON.stringify(data.params ?? {}, null, 2),
        );
        detail.id = detailId;

        toggle.addEventListener("click", () => {
          const expanded = toggle.getAttribute("aria-expanded") === "true";
          toggle.setAttribute("aria-expanded", String(!expanded));
          toggle.textContent = expanded ? "查看参数" : "收起参数";
          detail.classList.toggle("hidden", expanded);
        });

        content.append(
          document.createTextNode("调用工具："),
          toolName,
          toggle,
          detail,
        );
        appendStep(step);
        return;
      }

      if (data.step === "tool_done") {
        const state = data.success ? "success" : "failure";
        const { step, content } = createStep(state);
        content.append(
          document.createTextNode("工具 "),
          SafeRender.createElement("b", "", data.tool ?? "未知工具"),
          document.createTextNode(data.success ? " 已完成" : " 执行失败"),
        );
        appendStep(step);
        return;
      }

      if (data.chunk && data.chunk.length > 0) {
        loading.classList.remove("active");
        answerCard.classList.remove("hidden");
        chunkQueue.push(data.chunk);
        if (!animating) {
          animating = true;
          activeAnimationFrameId = requestAnimationFrame(renderFrame);
        }
        return;
      }

      if (data.done) {
        finished = true;
        completionData = data;
        if (data.iterations) iterationCount = data.iterations;
        if (!animating) finishRendering();
      }
    }

    try {
      const response = await fetch("/agent/chat/stream", {
        method: "POST",
        headers: {
          Accept: "text/event-stream",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ message, session_id: sessionId }),
        signal: controller.signal,
      });
      if (!response.ok || !response.body) {
        throw new Error("Agent stream request failed");
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { value, done } = await reader.read();
        buffer += decoder.decode(value, { stream: !done });
        let boundary = buffer.indexOf("\n\n");

        while (boundary >= 0) {
          const block = buffer.slice(0, boundary);
          buffer = buffer.slice(boundary + 2);
          const data = block
            .split("\n")
            .filter((line) => line.startsWith("data: "))
            .map((line) => line.slice(6))
            .join("\n");
          if (data) handleEvent(JSON.parse(data));
          boundary = buffer.indexOf("\n\n");
        }

        if (done) break;
      }

      if (!finished && !controller.signal.aborted) {
        throw new Error("Agent stream ended without completion");
      }
    } catch (requestError) {
      if (requestError.name === "AbortError" || finished) return;
      if (activeRequestController === controller) activeRequestController = null;
      setRunning(false);
      answerEl.classList.remove("streaming");
      showError("连接中断或 Agent 处理失败，请稍后重试。");
    }
  }

  async function newSession() {
    if (!confirm("新建会话会清除当前 Agent 历史，是否继续？")) return;

    stopAgent(false);
    newSessionBtn.disabled = true;
    clearError();

    try {
      const response = await fetch(
        "/agent/reset?session_id=" + encodeURIComponent(sessionId),
        { method: "POST" },
      );
      if (!response.ok && response.status !== 404) {
        throw new Error("Agent session reset failed");
      }

      sessionId = crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).slice(2, 14);
      localStorage.setItem(SESSION_KEY, sessionId);
      thinking.replaceChildren();
      processPanel.classList.add("hidden");
      answerEl.textContent = "";
      answerCard.classList.add("hidden");
      messageInput.value = "";
      latencyEl.textContent = "—";
      iterationEl.textContent = "—";
      messageInput.focus();
    } catch {
      showError("无法清除当前会话，请稍后重试。");
    } finally {
      newSessionBtn.disabled = false;
    }
  }

  agentForm.addEventListener("submit", (event) => {
    event.preventDefault();
    send();
  });
  stopBtn.addEventListener("click", () => stopAgent(true));
  newSessionBtn.addEventListener("click", newSession);
  messageInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      agentForm.requestSubmit();
    }
    if (event.key === "Escape" && activeRequestController) stopAgent(true);
  });
})();
