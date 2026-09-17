(() => {
  "use strict";

  const SESSION_KEY = "ragflow_session_id";
  const WARNING_MESSAGES = Object.freeze({
    bm25_unavailable: "关键词检索暂不可用，当前结果仅使用向量检索。",
    bm25_version_mismatch: "关键词索引与当前向量索引版本不一致，已自动降级。",
    rerank_timeout: "精排处理超时，当前结果使用融合排序。",
    rerank_unavailable: "精排服务暂不可用，当前结果使用融合排序。",
  });

  const searchForm = document.getElementById("searchForm");
  const queryInput = document.getElementById("queryInput");
  const searchBtn = document.getElementById("searchBtn");
  const stopBtn = document.getElementById("stopBtn");
  const newSessionBtn = document.getElementById("newSessionBtn");
  const loading = document.getElementById("loading");
  const result = document.getElementById("result");
  const error = document.getElementById("error");
  const answerEl = document.getElementById("answer");
  const sourcesEl = document.getElementById("sources");
  const warningList = document.getElementById("warningList");
  const latencyEl = document.getElementById("latency");
  const indexVersionEl = document.getElementById("indexVersion");
  let sessionId = localStorage.getItem(SESSION_KEY);
  let currentEventSource = null;
  let currentAnimationFrameId = null;

  if (!sessionId) {
    sessionId = crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).slice(2, 14);
    localStorage.setItem(SESSION_KEY, sessionId);
  }

  function setRunning(running) {
    searchForm.setAttribute("aria-busy", String(running));
    queryInput.readOnly = running;
    searchBtn.classList.toggle("hidden", running);
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

  function stopSearch(showMessage = true) {
    if (currentEventSource) {
      currentEventSource.close();
      currentEventSource = null;
    }
    if (currentAnimationFrameId !== null) {
      cancelAnimationFrame(currentAnimationFrameId);
      currentAnimationFrameId = null;
    }

    setRunning(false);
    answerEl.classList.remove("streaming");

    if (showMessage) {
      result.classList.add("active");
      if (!answerEl.textContent) answerEl.textContent = "已停止生成。";
      latencyEl.textContent = "—";
    }
  }

  function renderSources(sources) {
    const fragment = document.createDocumentFragment();

    sources.forEach((source, index) => {
      const item = source && typeof source === "object" ? source : {};
      const text = String(item.text ?? "");
      const numericScore = Number(item.score);
      const score = Number.isFinite(numericScore) ? numericScore.toFixed(4) : "—";

      const card = SafeRender.createElement("article", "source-card");
      const meta = SafeRender.createElement("div", "source-meta");
      meta.append(
        SafeRender.createElement("span", "source-ref", "[" + String(item.ref ?? index + 1) + "]"),
        SafeRender.createElement("span", "source-file", item.source ?? "未知来源"),
        SafeRender.createElement("span", "source-score", "相关度 " + score),
      );
      card.append(
        meta,
        SafeRender.createElement(
          "div",
          "source-text",
          text.slice(0, 360) + (text.length > 360 ? "…" : ""),
        ),
      );
      fragment.appendChild(card);
    });

    if (sources.length === 0) {
      fragment.appendChild(
        SafeRender.createElement("p", "sources-empty", "本次回答没有返回引用来源。"),
      );
    }

    sourcesEl.replaceChildren(fragment);
  }

  function renderWarnings(warnings) {
    const fragment = document.createDocumentFragment();

    warnings.forEach((warning) => {
      const code = String(warning);
      fragment.appendChild(
        SafeRender.createElement(
          "div",
          "notice notice-warning",
          WARNING_MESSAGES[code] ?? "检索过程发生降级：" + code,
        ),
      );
    });

    warningList.replaceChildren(fragment);
  }

  function search() {
    const query = queryInput.value.trim();
    if (!query) {
      queryInput.focus();
      return;
    }

    stopSearch(false);
    clearError();
    setRunning(true);
    answerEl.textContent = "";
    answerEl.classList.add("streaming");
    sourcesEl.replaceChildren();
    warningList.replaceChildren();
    document.getElementById("sourceCount").textContent = "0";
    latencyEl.textContent = "—";
    indexVersionEl.textContent = "—";
    result.classList.add("active");

    const url = "/query/stream?query=" + encodeURIComponent(query)
      + "&session_id=" + encodeURIComponent(sessionId);
    const eventSource = new EventSource(url);
    currentEventSource = eventSource;

    const typingInterval = 35;
    const chunkQueue = [];
    let fullAnswer = "";
    let animating = false;
    let finished = false;
    let completionData = null;
    let lastRenderTime = 0;
    let firstRender = true;

    function finishRendering() {
      if (currentEventSource !== eventSource) return;

      answerEl.classList.remove("streaming");
      eventSource.close();
      currentEventSource = null;
      currentAnimationFrameId = null;
      setRunning(false);

      const latency = Number(completionData?.latency_ms);
      latencyEl.textContent = Number.isFinite(latency) ? Math.round(latency) + " ms" : "—";
      indexVersionEl.textContent = String(completionData?.index_version ?? "—");
    }

    function renderFrame(timestamp) {
      currentAnimationFrameId = null;
      if (currentEventSource !== eventSource) return;

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
      currentAnimationFrameId = requestAnimationFrame(renderFrame);
    }

    eventSource.onmessage = (event) => {
      let data;
      try {
        data = JSON.parse(event.data);
      } catch {
        eventSource.close();
        currentEventSource = null;
        setRunning(false);
        answerEl.classList.remove("streaming");
        showError("服务返回了无法解析的数据，请稍后重试。");
        return;
      }

      if (data.error) {
        const errorMessage = typeof data.error === "object"
          ? data.error.message
          : data.error;
        eventSource.close();
        currentEventSource = null;
        setRunning(false);
        answerEl.classList.remove("streaming");
        showError(errorMessage || "回答生成失败，请稍后重试。");
        return;
      }

      if (data.chunk && data.chunk.length > 0) {
        loading.classList.remove("active");
        chunkQueue.push(data.chunk);
        if (!animating) {
          animating = true;
          currentAnimationFrameId = requestAnimationFrame(renderFrame);
        }
      }

      if (data.done) {
        finished = true;
        completionData = data;
        const sources = Array.isArray(data.sources) ? data.sources : [];
        const warnings = Array.isArray(data.warnings) ? data.warnings : [];
        document.getElementById("sourceCount").textContent = String(sources.length);
        renderSources(sources);
        renderWarnings(warnings);
        if (!animating) finishRendering();
      }
    };

    eventSource.onerror = () => {
      if (finished || currentEventSource !== eventSource) return;
      eventSource.close();
      currentEventSource = null;
      setRunning(false);
      answerEl.classList.remove("streaming");
      showError("连接中断或查询失败，请稍后重试。");
    };
  }

  async function newSession() {
    if (!confirm("新建会话会清除当前检索历史，是否继续？")) return;

    stopSearch(false);
    newSessionBtn.disabled = true;
    clearError();

    try {
      const response = await fetch(
        "/session/reset?session_id=" + encodeURIComponent(sessionId),
        { method: "POST" },
      );
      if (!response.ok && response.status !== 404) {
        throw new Error("Session reset failed");
      }

      sessionId = crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).slice(2, 14);
      localStorage.setItem(SESSION_KEY, sessionId);
      result.classList.remove("active");
      answerEl.textContent = "";
      sourcesEl.replaceChildren();
      warningList.replaceChildren();
      queryInput.value = "";
      latencyEl.textContent = "—";
      indexVersionEl.textContent = "—";
      queryInput.focus();
    } catch {
      showError("无法清除当前会话，请稍后重试。");
    } finally {
      newSessionBtn.disabled = false;
    }
  }

  searchForm.addEventListener("submit", (event) => {
    event.preventDefault();
    search();
  });
  stopBtn.addEventListener("click", () => stopSearch(true));
  newSessionBtn.addEventListener("click", newSession);
  queryInput.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && currentEventSource) stopSearch(true);
  });
})();
