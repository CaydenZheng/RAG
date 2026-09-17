(() => {
  "use strict";

  const HTTP_URL_PATTERN = /(https?:\/\/[^\s<>"']+)/g;
  const READINESS_LABELS = Object.freeze({
    ready: "服务可用",
    degraded: "部分能力降级",
    starting: "服务准备中",
    unavailable: "服务暂不可用",
  });

  function createElement(tagName, className, text) {
    const element = document.createElement(tagName);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = String(text);
    return element;
  }

  function appendLinkifiedText(container, value) {
    const text = String(value ?? "");
    const fragment = document.createDocumentFragment();
    let cursor = 0;

    for (const match of text.matchAll(HTTP_URL_PATTERN)) {
      if (match.index > cursor) {
        fragment.appendChild(document.createTextNode(text.slice(cursor, match.index)));
      }

      const url = match[0];
      const link = createElement("a", "", url);
      link.href = url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      fragment.appendChild(link);
      cursor = match.index + url.length;
    }

    if (cursor < text.length) {
      fragment.appendChild(document.createTextNode(text.slice(cursor)));
    }

    container.replaceChildren(fragment);
  }

  async function initServiceStatus() {
    const status = document.getElementById("serviceStatus");
    const statusText = document.getElementById("serviceStatusText");
    if (!status || !statusText) return;

    try {
      const response = await fetch("/ready", {
        headers: { Accept: "application/json" },
        cache: "no-store",
      });
      const snapshot = await response.json().catch(() => ({}));
      const reportedState = String(snapshot.status ?? "").toLowerCase();
      const state = Object.hasOwn(READINESS_LABELS, reportedState)
        ? reportedState
        : response.ok ? "ready" : "unavailable";
      status.dataset.state = state;
      statusText.textContent = READINESS_LABELS[state];
    } catch {
      status.dataset.state = "unavailable";
      statusText.textContent = READINESS_LABELS.unavailable;
    }
  }

  window.SafeRender = Object.freeze({ createElement, appendLinkifiedText });
  initServiceStatus();
})();
