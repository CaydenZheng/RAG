"""Configuration and startup readiness must fail clearly without real models."""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import Response
from pydantic import ValidationError

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
ENV_TEMPLATE = REPOSITORY_ROOT / ".env.example"
ENV_NAME = re.compile(r"^\s*(?:#\s*)?([A-Z][A-Z0-9_]*)=")


def _template_names() -> set[str]:
    names: set[str] = set()
    for line in ENV_TEMPLATE.read_text(encoding="utf-8").splitlines():
        match: re.Match[str] | None = ENV_NAME.match(line)
        if match is not None:
            names.add(match.group(1))
    return names


def test_env_template_matches_settings_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from config.settings import Settings

    aliases: set[str] = {
        field.alias or name for name, field in Settings.model_fields.items()
    }
    assert _template_names() == aliases

    for alias in aliases:
        monkeypatch.delenv(alias, raising=False)
    configured: Settings = Settings(_env_file=ENV_TEMPLATE)
    assert configured.openai_base_url == "https://api.deepseek.com"
    assert configured.llm_model == "deepseek-chat"
    assert configured.local_embedding_model == "BAAI/bge-base-en-v1.5"
    assert configured.startup_preload_reranker is True


def test_settings_reject_missing_or_blank_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from config.settings import Settings

    monkeypatch.delenv("OPENAI_API_KEY")
    with pytest.raises(ValidationError, match="OPENAI_API_KEY"):
        Settings(_env_file=None)

    monkeypatch.setenv("OPENAI_API_KEY", "   ")
    with pytest.raises(ValidationError, match="must not be blank"):
        Settings(_env_file=None)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("RRF_K", "0"),
        ("VECTOR_TOP_K", "0"),
        ("BM25_TOP_K", "-1"),
        ("RERANK_TOP_K", "0"),
        ("MAX_CONTEXT_TOKENS", "255"),
        ("SYSTEM_RESERVE_RATIO", "1"),
        ("CONTEXT_BUFFER_RATIO", "-0.01"),
    ],
)
def test_settings_reject_invalid_resource_limits(name: str, value: str) -> None:
    from config.settings import Settings

    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{name: value})


def test_settings_reject_exhausted_context_budget() -> None:
    from config.settings import Settings

    with pytest.raises(ValidationError, match="context reserve ratios"):
        Settings(
            _env_file=None,
            SYSTEM_RESERVE_RATIO=0.8,
            CONTEXT_BUFFER_RATIO=0.2,
        )


def test_warmup_runs_required_components_before_optional_reranker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.api import startup

    calls: list[str] = []

    def preload_embedding() -> bool:
        calls.append("embedding")
        return True

    def prepare_index() -> bool:
        calls.append("index")
        return True

    def preload_reranker() -> bool:
        calls.append("reranker")
        return True

    monkeypatch.setattr(startup, "_preload_embedding", preload_embedding)
    monkeypatch.setattr(startup, "_prepare_index", prepare_index)
    monkeypatch.setattr(startup, "_preload_reranker", preload_reranker)
    monkeypatch.setattr(
        startup.settings, "startup_preload_reranker", True, raising=False
    )

    startup._warm_up_components()

    assert calls == ["embedding", "index", "reranker"]


def test_low_memory_failure_is_public_and_actionable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.api import startup
    from src.llm import llm_client

    def raise_memory_error(instance: object) -> int:
        raise MemoryError

    monkeypatch.setattr(
        type(llm_client),
        "embedding_dim",
        property(raise_memory_error),
    )
    startup.runtime_readiness.reset()

    assert startup._preload_embedding() is False

    snapshot: dict[str, Any] = startup.runtime_readiness.snapshot()
    embedding: dict[str, Any] = snapshot["components"]["embedding"]
    assert snapshot["status"] == "unavailable"
    assert embedding["status"] == "failed"
    assert embedding["code"] == "insufficient_memory"
    assert "smaller model" in embedding["message"]


def test_optional_reranker_preload_can_be_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.api import startup

    def required_component_ready() -> bool:
        return True

    def unexpected_reranker_load() -> bool:
        pytest.fail("reranker should not be preloaded")

    monkeypatch.setattr(startup, "_preload_embedding", required_component_ready)
    monkeypatch.setattr(startup, "_prepare_index", required_component_ready)
    monkeypatch.setattr(startup, "_preload_reranker", unexpected_reranker_load)
    monkeypatch.setattr(
        startup.settings, "startup_preload_reranker", False, raising=False
    )
    startup.runtime_readiness.reset()

    startup._warm_up_components()

    snapshot: dict[str, Any] = startup.runtime_readiness.snapshot()
    assert snapshot["components"]["reranker"]["status"] == "skipped"


def test_readiness_endpoint_requires_embedding_and_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tiktoken

    monkeypatch.setattr(
        tiktoken,
        "get_encoding",
        lambda name: SimpleNamespace(encode=lambda text: list(text)),
    )

    import app as api

    api.runtime_readiness.reset()
    starting_response: Response = Response()
    starting: dict[str, Any] = api.readiness(starting_response)
    assert starting_response.status_code == 503
    assert starting["status"] == "starting"

    api.runtime_readiness.update("embedding", "ready", message="ready")
    api.runtime_readiness.update("index", "ready", message="ready")
    api.runtime_readiness.update(
        "bm25",
        "degraded",
        code="bm25_unavailable",
        message="vector retrieval remains available",
    )
    api.runtime_readiness.update("reranker", "ready", message="ready")

    degraded_response: Response = Response()
    degraded: dict[str, Any] = api.readiness(degraded_response)
    assert degraded_response.status_code == 200
    assert degraded["status"] == "degraded"
