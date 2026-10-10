"""Regression coverage for the built browser application and page policy."""

from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

FRONTEND_SOURCE_DIR = Path(__file__).parents[2] / "frontend" / "src"
FORBIDDEN_SOURCE_SINKS = (
    ".innerHTML",
    ".outerHTML",
    "insertAdjacentHTML",
    "document.write",
    "dangerouslySetInnerHTML",
    "rehypeRaw",
    "eval(",
    "new Function",
)


class PageMarkupAudit(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.inline_handlers: list[str] = []
        self.inline_styles: list[str] = []
        self.inline_script_count = 0
        self.inline_style_count = 0
        self.asset_paths: list[str] = []
        self.root_count = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        self.inline_handlers.extend(name for name in attributes if name.startswith("on"))
        if "style" in attributes:
            self.inline_styles.append(attributes["style"] or "")
        if tag == "script":
            source = attributes.get("src")
            if source:
                self.asset_paths.append(source)
            else:
                self.inline_script_count += 1
        if tag == "link" and attributes.get("rel") == "stylesheet":
            href = attributes.get("href")
            if href:
                self.asset_paths.append(href)
        if tag == "style":
            self.inline_style_count += 1
        if tag == "div" and attributes.get("id") == "root":
            self.root_count += 1


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


def _audit_page(page_text: str) -> PageMarkupAudit:
    audit = PageMarkupAudit()
    audit.feed(page_text)
    return audit


def test_browser_pages_discover_and_serve_built_assets(
    web_client: TestClient,
) -> None:
    try:
        page_bodies: list[str] = []
        for page_path in ("/", "/agent"):
            page = web_client.get(page_path)
            assert page.status_code == 200
            assert page.headers["content-type"].startswith("text/html")
            audit = _audit_page(page.text)
            assert audit.root_count == 1
            assert len(audit.asset_paths) >= 2
            assert all(
                path.startswith("/static/app/") for path in audit.asset_paths
            )
            for asset_path in audit.asset_paths:
                asset = web_client.get(asset_path)
                assert asset.status_code == 200
            page_bodies.append(page.text)

        assert page_bodies[0] == page_bodies[1]
    finally:
        web_client.close()


def test_pages_enforce_external_only_active_content(
    web_client: TestClient,
) -> None:
    try:
        for page_path in ("/", "/agent"):
            response = web_client.get(page_path)
            policy = response.headers["content-security-policy"]
            assert "script-src 'self'" in policy
            assert "style-src 'self'" in policy
            assert "object-src 'none'" in policy
            assert "base-uri 'none'" in policy
            assert "frame-ancestors 'none'" in policy
            assert "'unsafe-inline'" not in policy
            assert "'unsafe-eval'" not in policy
            assert response.headers["referrer-policy"] == "no-referrer"
            assert response.headers["x-content-type-options"] == "nosniff"

            audit = _audit_page(response.text)
            assert audit.inline_handlers == []
            assert audit.inline_styles == []
            assert audit.inline_script_count == 0
            assert audit.inline_style_count == 0
    finally:
        web_client.close()


def test_frontend_source_has_no_html_injection_sinks() -> None:
    source_files = sorted(
        path
        for path in FRONTEND_SOURCE_DIR.rglob("*")
        if path.suffix in {".ts", ".tsx"} and "test" not in path.parts
    )
    assert source_files
    for source_file in source_files:
        source = source_file.read_text(encoding="utf-8")
        assert not any(sink in source for sink in FORBIDDEN_SOURCE_SINKS)


def test_missing_build_returns_503_without_breaking_business_routes(
    web_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import src.web.pages as pages

    monkeypatch.setattr(pages, "APP_INDEX_PATH", tmp_path / "missing.html")
    try:
        assert web_client.get("/health").status_code == 200
        for page_path in ("/", "/agent"):
            response = web_client.get(page_path)
            assert response.status_code == 503
            assert "npm ci" in response.text
            assert "npm run build" in response.text
            assert "'unsafe-inline'" not in response.headers[
                "content-security-policy"
            ]
    finally:
        web_client.close()
