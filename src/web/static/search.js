(() => {
  "use strict";

  const SESSION_KEY = "ragflow_session_id";
  const queryInput = document.getElementById("queryInput");
  const searchBtn = document.getElementById("searchBtn");
  const stopBtn = document.getElementById("stopBtn");
  const loading = document.getElementById("loading");
  const result = document.getElementById("result");
  const error = document.getElementById("error");
  const answerEl = document.getElementById("answer");
  const sourcesEl = document.getElementById("sources");
  let sessionId = localStorage.getItem(SESSION_KEY);
  let currentEventSource = null;

  if (!sessionId) {
    sessionId = crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).slice(2, 14);
    localStorage.setItem(SESSION_KEY, sessionId);
  }

  function stopSearch() {
    if (currentEventSource) {
      currentEventSource.close();
      currentEventSource = null;
    }
    searchBtn.disabled = false;
    searchBtn.classList.remove("hidden");
    stopBtn.classList.remove("active");
    loading.classList.remove("active");
    answerEl.classList.remove("streaming");
    if (!answerEl.textContent) answerEl.textContent = "⏹ 已终止";
    document.getElementById("latency").textContent = "—";
  }

  function renderSources(sources) {
    const fragment = document.createDocumentFragment();

    sources.forEach((source, index) => {
      const item = source && typeof source === "object" ? source : {};
      const text = String(item.text ?? "");
      const numericScore = Number(item.score);
      const score = Number.isFinite(numericScore) ? numericScore.toFixed(4) : "—";

      const card = SafeRender.createElement("div", "source-card");
      const meta = SafeRender.createElement("div", "source-meta");
      meta.append(
        SafeRender.createElement("span", "source-ref", `[${item.ref ?? index + 1}]`),
        SafeRender.createElement("span", "source-file", item.source ?? "unknown"),
        SafeRender.createElement("span", "source-score", score),
      );
      card.append(
        meta,
        SafeRender.createElement(
          "div",
          "source-text",
          text.slice(0, 300) + (text.length > 300 ? "..." : ""),
        ),
      );
      fragment.appendChild(card);
    });

    sourcesEl.replaceChildren(fragment);
  }

  function search() {
    const query = queryInput.value.trim();
    if (!query) return;

    stopSearch();
    searchBtn.classList.add("hidden");
    stopBtn.classList.add("active");
    loading.classList.add("active");
    error.classList.remove("active");
    answerEl.textContent = "";
    answerEl.classList.add("streaming");
    sourcesEl.replaceChildren();
    document.getElementById("sourceCount").textContent = "0";
    result.classList.add("active");

    const url = "/query/stream?query=" + encodeURIComponent(query) +
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

    function finishRendering() {
      answerEl.classList.remove("streaming");
      eventSource.close();
      currentEventSource = null;
      searchBtn.classList.remove("hidden");
      stopBtn.classList.remove("active");
      loading.classList.remove("active");
      result.classList.add("active");
      document.getElementById("latency").textContent = Date.now() - startTime;
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
        searchBtn.classList.remove("hidden");
        stopBtn.classList.remove("active");
        currentEventSource = null;
        eventSource.close();
        return;
      }

      if (data.chunk && data.chunk.length > 0) {
        if (!animating) loading.classList.remove("active");
        chunkQueue.push(data.chunk);
        if (!animating) {
          animating = true;
          requestAnimationFrame(renderFrame);
        }
      }

      if (data.done) {
        finished = true;
        const sources = Array.isArray(data.sources) ? data.sources : [];
        document.getElementById("sourceCount").textContent = String(sources.length);
        renderSources(sources);
        if (!animating) finishRendering();
      }
    };

    eventSource.onerror = () => {
      if (finished) return;
      eventSource.close();
      currentEventSource = null;
      loading.classList.remove("active");
      answerEl.classList.remove("streaming");
      searchBtn.classList.remove("hidden");
      stopBtn.classList.remove("active");
      error.textContent = "⚠️ 连接中断或查询失败，请稍后重试";
      error.classList.add("active");
    };
  }

  function newSession(event) {
    event.preventDefault();
    if (confirm("开始新会话？当前对话历史将被清除。")) {
      sessionId = crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).slice(2, 14);
      localStorage.setItem(SESSION_KEY, sessionId);
      result.classList.remove("active");
      queryInput.value = "";
      queryInput.focus();
    }
  }

  searchBtn.addEventListener("click", search);
  stopBtn.addEventListener("click", stopSearch);
  document.getElementById("newSessionLink").addEventListener("click", newSession);
  queryInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") search();
  });
})();