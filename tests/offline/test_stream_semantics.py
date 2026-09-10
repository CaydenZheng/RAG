"""Regression tests for RAG SSE completion, errors, and disconnects."""

import asyncio
import json
import sys
import time
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


def _data_events(response_text: str) -> list[dict]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in response_text.splitlines()
        if line.startswith("data: ")
    ]


def test_stream_emits_common_envelope_and_complete_response(
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app as api
    from src.core import generation

    class RetrievalFlow:
        async def run_async(self, shared: dict) -> None:
            shared.update(
                context="prepared context",
                history=[],
                sources=[{"ref": 1, "chunk_id": "chunk-1"}],
                valid_citation_refs={1},
            )

    async def stream_answer(*args, **kwargs):
        yield "answer "
        yield "[1]"

    monkeypatch.setattr(api, "get_retrieval_flow", RetrievalFlow)
    monkeypatch.setattr(generation.llm_client, "chat_stream_async", stream_answer)

    client = TestClient(api.app)
    try:
        response = client.get("/query/stream", params={"query": "question"})
    finally:
        client.close()

    events = _data_events(response.text)
    chunks = [event for event in events if event["event"] == "chunk"]
    completed = events[-1]

    assert response.status_code == 200
    assert chunks
    assert all(event["done"] is False for event in chunks)
    assert len({event["query_id"] for event in events}) == 1
    assert completed["event"] == "done"
    assert completed["done"] is True
    assert completed["answer"] == "answer [1]"
    assert completed["sources"] == [{"ref": 1, "chunk_id": "chunk-1"}]
    assert completed["session_id"] == ""
    assert completed["latency_ms"] >= 0


def test_stream_failure_emits_one_safe_terminal_error(
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app as api
    from src.core import generation

    class RetrievalFlow:
        async def run_async(self, shared: dict) -> None:
            shared.update(
                context="prepared context",
                history=[],
                sources=[],
                valid_citation_refs=set(),
            )

    async def fail_stream(*args, **kwargs):
        if False:
            yield ""
        raise RuntimeError("provider-secret C:\\private\\model")

    monkeypatch.setattr(api, "get_retrieval_flow", RetrievalFlow)
    monkeypatch.setattr(generation.llm_client, "chat_stream_async", fail_stream)

    client = TestClient(api.app)
    try:
        response = client.get("/query/stream", params={"query": "question"})
    finally:
        client.close()

    events = _data_events(response.text)

    assert response.status_code == 200
    assert events == [
        {
            "event": "error",
            "query_id": events[0]["query_id"],
            "done": True,
            "error": {
                "code": "answer_generation_failed",
                "message": "回答生成失败，请稍后重试",
            },
        }
    ]
    assert "provider-secret" not in response.text
    assert "private" not in response.text


def test_disconnect_closes_generation_without_persisting_partial_answer(
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.api.streaming import iter_answer_sse
    from src.core import generation
    from src.core.generation import AnswerInput, AnswerService

    provider_closed = False
    persisted = False

    async def stream_answer(*args, **kwargs):
        nonlocal provider_closed
        try:
            yield "partial answer"
            await asyncio.Event().wait()
        finally:
            provider_closed = True

    def append_exchange(*args, **kwargs):
        nonlocal persisted
        persisted = True

    class DisconnectedRequest:
        async def is_disconnected(self) -> bool:
            return True

    monkeypatch.setattr(generation.llm_client, "chat_stream_async", stream_answer)
    monkeypatch.setattr(
        generation,
        "session_store",
        SimpleNamespace(
            append_exchange=append_exchange,
            history_count=lambda session_id: 1,
        ),
    )

    async def consume() -> list[str]:
        return [
            event
            async for event in iter_answer_sse(
                request=DisconnectedRequest(),
                service=AnswerService(),
                answer_input=AnswerInput(
                    query="question",
                    context="context",
                    history=[],
                    session_id="private-session",
                    valid_citation_refs=frozenset(),
                ),
                sources=[],
                public_session_id="public-session",
                query_id="query-id",
                started_at=time.perf_counter(),
            )
        ]

    events = asyncio.run(consume())

    assert events == ["retry: 3000\n\n"]
    assert provider_closed is True
    assert persisted is False
