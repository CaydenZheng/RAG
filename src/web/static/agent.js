(() => {
  "use strict";

  const SESSION_KEY = "ragflow_agent_session";
  const messageInput = document.getElementById("msgInput");
  const sendBtn = document.getElementById("sendBtn");
  const stopBtn = document.getElementById("stopBtn");
  const loading = document.getElementById("loading");
  const error = document.getElementById("error");
  const thinking = document.getElementById("thinking");
  const answerEl = document.getElementById("answer");
  const answerCard = document.getElementById("answerCard");
  let sessionId = localStorage.getItem(SESSION_KEY);
  let currentEventSource = null;

  if (!sessionId) {
    sessionId = crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).slice(2, 14);
    localStorage.setItem(SESSION_KEY, sessionId);
  }

  function createStep(icon) {
    const step = SafeRender.createElement("div", "step");
    const content = SafeRender.createElement("div", "step-content");
    step.append(SafeRender.createElement("span", "step-icon", icon), content);
    return { step, content };
  }

  function appendStep(step) {
    thinking.appendChild(step);
    thinking.scrollTop = thinking.scrollHeight;
  }

  function stopAgent() {
    if (currentEventSource) {
      currentEventSource.close();
      currentEventSource = null;
    }
    sendBtn.classList.remove("hidden");
    stopBtn.classList.remove("active");
    loading.classList.remove("active");
    answerEl.classList.remove("streaming");
    if (!answerEl.textContent) {
      answerEl.textContent = "⏹ 已终止";
      answerCard.classList.remove("hidden");
    }
  }

  function send() {
    const message = messageInput.value.trim();
    if (!message) return;

    stopAgent();
    sendBtn.classList.add("hidden");
    stopBtn.classList.add("active");
    loading.classList.add("active");
    error.classList.remove("active");
    error.textContent = "";
    thinking.replaceChildren();
    answerEl.textContent = "";
    answerEl.classList.add("streaming");
    answerEl.classList.remove("placeholder");
    answerCard.classList.add("hidden");
    document.getElementById("latency").textContent = "—";
    document.getElementById("iterCount").textContent = "—";

    const url = "/agent/chat/stream?message=" + encodeURIComponent(message) +
      "&session_id=" + encodeURIComponent(sessionId);
    const eventSource = new EventSource(url);
    currentEventSource = eventSource;

    const typingInterval = 35;
    const chunkQueue = [];
    let fullAnswer = "";
    let animating = false;
    let finished = false;
    let lastRenderTime = 0;
    let firstRender = true;
    const startTime = Date.now();
    let iterationCount = 0;

    function finishRendering() {
      answerEl.classList.remove("streaming");
      SafeRender.appendLinkifiedText(answerEl, fullAnswer);
      eventSource.close();
      currentEventSource = null;
      sendBtn.classList.remove("hidden");
      stopBtn.classList.remove("active");
      loading.classList.remove("active");
      answerCard.classList.remove("hidden");
      document.getElementById("latency").textContent = `${Date.now() - startTime} ms`;
      document.getElementById("iterCount").textContent = String(iterationCount);
    }

    function renderFrame(timestamp) {
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
      requestAnimationFrame(renderFrame);
    }

    eventSource.onmessage = (event) => {
      const data = JSON.parse(event.data);

      if (data.error) {
        loading.classList.remove("active");
        answerEl.classList.remove("streaming");
        error.textContent = "⚠️ " + data.error;
        error.classList.add("active");
        sendBtn.classList.remove("hidden");
        stopBtn.classList.remove("active");
        currentEventSource = null;
        eventSource.close();
        return;
      }

      if (data.step === "planning") {
        loading.classList.remove("active");
        iterationCount = data.iteration;
        const { step, content } = createStep("🤔");
        content.textContent = `第 ${data.iteration} 轮规划中…`;
        appendStep(step);
        return;
      }

      if (data.step === "tool_call") {
        const { step, content } = createStep("🔧");
        const toolName = SafeRender.createElement("b", "", data.tool ?? "unknown");
        const detail = SafeRender.createElement(
          "div",
          "step-detail collapsed",
          JSON.stringify(data.params ?? {}, null, 2),
        );
        detail.addEventListener("click", () => detail.classList.toggle("collapsed"));
        content.append(
          document.createTextNode("调用工具: "),
          toolName,
          detail,
        );
        appendStep(step);
        return;
      }

      if (data.step === "tool_done") {
        const { step, content } = createStep(data.success ? "✅" : "❌");
        content.append(
          document.createTextNode("工具 "),
          SafeRender.createElement("b", "", data.tool ?? "unknown"),
          document.createTextNode(data.success ? " 完成" : " 失败"),
        );
        appendStep(step);
        return;
      }

      if (data.chunk && data.chunk.length > 0) {
        if (!animating) {
          answerCard.classList.remove("hidden");
          loading.classList.remove("active");
        }
        chunkQueue.push(data.chunk);
        if (!animating) {
          animating = true;
          requestAnimationFrame(renderFrame);
        }
        return;
      }

      if (data.done) {
        finished = true;
        if (data.iterations) iterationCount = data.iterations;
        if (!animating) finishRendering();
      }
    };

    eventSource.onerror = () => {
      if (finished) return;
      eventSource.close();
      currentEventSource = null;
      loading.classList.remove("active");
      answerEl.classList.remove("streaming");
      sendBtn.classList.remove("hidden");
      stopBtn.classList.remove("active");
      error.textContent = "⚠️ 连接中断或 Agent 处理失败，请稍后重试";
      error.classList.add("active");
    };
  }

  function newSession(event) {
    event.preventDefault();
    if (confirm("开始新会话？当前对话历史将被清除。")) {
      sessionId = crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).slice(2, 14);
      localStorage.setItem(SESSION_KEY, sessionId);
      thinking.replaceChildren();
      answerEl.textContent = "";
      answerCard.classList.add("hidden");
      messageInput.value = "";
      messageInput.focus();
      document.getElementById("latency").textContent = "—";
      document.getElementById("iterCount").textContent = "—";
    }
  }

  sendBtn.addEventListener("click", send);
  stopBtn.addEventListener("click", stopAgent);
  document.getElementById("newSessionLink").addEventListener("click", newSession);
  messageInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") send();
  });
})();