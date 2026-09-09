"""Regression coverage for browser pages and output-rendering policy."""

from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

FORBIDDEN_JS_SINKS = (
    ".innerHTML",
    ".outerHTML",
    "insertAdjacentHTML",
    "document.write",
    "eval(",
    "new Function",
)
PAGE_ASSETS = {
    "/": ("/static/common.css", "/static/search.css", "/static/rendering.js", "/static/search.js"),
    "/agent": ("/static/common.css", "/static/agent.css", "/static/rendering.js", "/static/agent.js"),
}


class PageMarkupAudit(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.inline_handlers: list[str] = []
        self.inline_styles: list[str] = []
        self.inline_script_count = 0
        self.inline_style_count = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        self.inline_handlers.extend(name for name in attributes if name.startswith("on"))
        if "style" in attributes:
            self.inline_styles.append(attributes["style"] or "")
        if tag == "script" and "src" not in attributes:
            self.inline_script_count += 1
        if tag == "style":
            self.inline_style_count += 1


@pytest.fixture
def web_client(
    monkeypatch: pytest.MonkeyPatch, isolated_runtime: Path
) -> TestClient:
    import tiktoken

    monkeypatch.setattr(
        tiktoken,
        "get_encoding",
        lambda name: SimpleNamespace(encode=lambda text: list(text)),
    )

    import app as api

    return TestClient(api.app)


def test_browser_pages_and_assets_are_available(web_client: TestClient) -> None:
    try:
        for page_path, asset_paths in PAGE_ASSETS.items():
            page = web_client.get(page_path)
            assert page.status_code == 200
            assert page.headers["content-type"].startswith("text/html")
            for asset_path in asset_paths:
                assert asset_path in page.text
                asset = web_client.get(asset_path)
                assert asset.status_code == 200
    finally:
        web_client.close()


def test_pages_enforce_external_only_active_content(web_client: TestClient) -> None:
    try:
        for page_path in PAGE_ASSETS:
            response = web_client.get(page_path)
            policy = response.headers["content-security-policy"]
            assert "script-src 'self'" in policy
            assert "style-src 'self'" in policy
            assert "object-src 'none'" in policy
            assert "base-uri 'none'" in policy
            assert "frame-ancestors 'none'" in policy
            assert "'unsafe-inline'" not in policy
            assert response.headers["referrer-policy"] == "no-referrer"
            assert response.headers["x-content-type-options"] == "nosniff"

            audit = PageMarkupAudit()
            audit.feed(response.text)
            assert audit.inline_handlers == []
            assert audit.inline_styles == []
            assert audit.inline_script_count == 0
            assert audit.inline_style_count == 0
    finally:
        web_client.close()


def test_browser_scripts_do_not_use_html_injection_sinks(
    web_client: TestClient,
) -> None:
    try:
        script_paths = sorted(
            path
            for asset_paths in PAGE_ASSETS.values()
            for path in asset_paths
            if path.endswith(".js")
        )
        for script_path in script_paths:
            script = web_client.get(script_path)
            assert script.status_code == 200
            assert not any(sink in script.text for sink in FORBIDDEN_JS_SINKS)
    finally:
        web_client.close()
