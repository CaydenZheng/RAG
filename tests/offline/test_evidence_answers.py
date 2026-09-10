"""Regression tests for the shared answer-input token budget."""

from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def fixed_tokenizer(monkeypatch: pytest.MonkeyPatch) -> None:
    import tiktoken

    monkeypatch.setattr(
        tiktoken,
        "get_encoding",
        lambda name: SimpleNamespace(encode=lambda text: list(text)),
    )


def test_context_history_and_evidence_share_one_budget(
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from config.settings import settings
    from src.core import generation

    history = [
        {"role": "user", "content": f"turn {number}"}
        for number in range(7)
    ]
    fake_store = SimpleNamespace(
        get_recent_history=lambda session_id, limit: history[-limit:]
    )
    monkeypatch.setattr(generation, "session_store", fake_store)
    monkeypatch.setattr(generation, "count_tokens", lambda text: len(text.split()))
    monkeypatch.setattr(settings, "max_context_tokens", 30)
    monkeypatch.setattr(settings, "system_reserve_ratio", 0.10)
    monkeypatch.setattr(settings, "context_buffer_ratio", 0.10)

    chunks = [
        {
            "chunk_id": "chunk-1",
            "text": "alpha beta",
            "rerank_score": 0.9,
            "metadata": {
                "doc_id": "doc-1",
                "source": "guide.md",
                "version": "v3",
                "chunk_index": 4,
                "page": 2,
            },
        },
        {
            "chunk_id": "chunk-2",
            "text": "lower ranked evidence",
            "rerank_score": 0.8,
            "metadata": {"source": "other.md", "chunk_index": 1},
        },
    ]

    result = generation.ContextBuilderNode().exec((chunks, "session"))

    budget = result["context_budget"]
    assert budget == {
        "total": 30,
        "available": 24,
        "history_tokens": 12,
        "evidence_tokens": 12,
    }
    assert budget["history_tokens"] + budget["evidence_tokens"] <= budget["available"]
    assert [message["content"] for message in result["history"]] == [
        "turn 1",
        "turn 2",
        "turn 3",
        "turn 4",
        "turn 5",
        "turn 6",
    ]
    assert result["sources"] == [
        {
            "ref": 1,
            "chunk_id": "chunk-1",
            "document_id": "doc-1",
            "source": "guide.md",
            "version": "v3",
            "chunk_index": 4,
            "position": {"chunk_index": 4, "page": 2},
            "text": "alpha beta",
            "score": 0.9,
        }
    ]
    assert "[1] Source: guide.md" in result["context"]
    assert "Version: v3" in result["context"]
    assert "Position: chunk_index=4, page=2" in result["context"]

