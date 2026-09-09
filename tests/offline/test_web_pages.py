"""Regression coverage for browser page resources."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient


def test_browser_pages_are_available(
    monkeypatch: pytest.MonkeyPatch, isolated_runtime: Path
) -> None:
    import tiktoken

    monkeypatch.setattr(
        tiktoken,
        "get_encoding",
        lambda name: SimpleNamespace(encode=lambda text: list(text)),
    )

    import app as api

    client = TestClient(api.app)
    try:
        search_page = client.get("/")
        agent_page = client.get("/agent")
    finally:
        client.close()

    assert search_page.status_code == 200
    assert search_page.headers["content-type"].startswith("text/html")
    assert '<div id="sources"></div>' in search_page.text

    assert agent_page.status_code == 200
    assert agent_page.headers["content-type"].startswith("text/html")
    assert '<div id="thinking"></div>' in agent_page.text
