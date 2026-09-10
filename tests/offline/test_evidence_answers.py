"""Regression tests for answer context budgets and evidence citations."""

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def fixed_tokenizer(monkeypatch: pytest.MonkeyPatch) -> None:
    import tiktoken

    monkeypatch.setattr(
        tiktoken,
        "get_encoding",
        lambda name: SimpleNamespace(encode=lambda text: list(text)),
    )


@pytest.fixture(autouse=True)
def reset_http_modules() -> None:
    for name in ("app", "src.infra.tracer"):
        sys.modules.pop(name, None)
    yield
    for name in ("app", "src.infra.tracer"):
        sys.modules.pop(name, None)


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
    assert result["valid_citation_refs"] == {1}
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


def test_normal_and_streaming_answers_share_messages_and_filter_citations(
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app as api
    from src.core.generation import GeneratorNode
    from src.infra import fallback

    history = [
        {"role": "user", "content": "previous question"},
        {"role": "assistant", "content": "previous answer"},
    ]
    normal_calls: list[list[dict[str, str]]] = []
    stream_calls: list[list[dict[str, str]]] = []

    async def normal_answer(messages, **kwargs):
        normal_calls.append(messages)
        return "normal [1, 99] forged [42]"

    async def stream_answer(messages, **kwargs):
        stream_calls.append(messages)
        for chunk in ("stream [", "1, ", "99]", " forged [", "42", "]"):
            yield chunk

    class RetrievalFlow:
        async def run_async(self, shared: dict) -> None:
            shared.update(
                context="prepared context",
                history=history,
                sources=[{"ref": 1, "chunk_id": "chunk-1"}],
                valid_citation_refs={1},
            )

    monkeypatch.setattr(fallback, "chat_with_fallback_async", normal_answer)
    monkeypatch.setattr(api, "get_retrieval_flow", RetrievalFlow)
    monkeypatch.setattr(api.llm_client, "chat_stream_async", stream_answer)

    normal, _ = asyncio.run(
        GeneratorNode().exec_async(
            ("question", "prepared context", history, "", {1})
        )
    )

    client = TestClient(api.app)
    try:
        response = client.get("/query/stream", params={"query": "question"})
    finally:
        client.close()

    events = [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    streamed_chunks = [
        event["chunk"] for event in events if "chunk" in event
    ]

    assert response.status_code == 200
    assert normal == "normal [1] forged "
    assert "".join(streamed_chunks) == "stream [1] forged "
    assert events[-1]["answer"] == "stream [1] forged "
    assert events[-1]["sources"] == [{"ref": 1, "chunk_id": "chunk-1"}]
    assert normal_calls == stream_calls
    assert normal_calls[0][1:3] == history
