(() => {
  "use strict";

  const HTTP_URL_PATTERN = /(https?:\/\/[^\s<>"']+)/g;

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

  window.SafeRender = Object.freeze({ createElement, appendLinkifiedText });
})();