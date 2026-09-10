"""Regression tests for RAG deadlines, capacity, and dependency degradation."""

import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def fixed_tokenizer(monkeypatch: pytest.MonkeyPatch) -> None:
    import tiktoken

    monkeypatch.setattr(
        tiktoken,
        "get_encoding",
        lambda name: SimpleNamespace(encode=lambda text: list(text)),
    )


def _scope(path: str) -> dict:
    return {
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": [],
        "query_string": b"",
        "state": {},
    }


async def _receive() -> dict:
    return {"type": "http.request", "body": b"", "more_body": False}


def test_stream_deadline_cancels_work_and_emits_terminal_timeout(
    isolated_runtime,
) -> None:
    from src.api.reliability import RAGRequestReliabilityMiddleware
    from src.api.streaming import answer_event, encode_sse_event

    provider_closed = False
    persisted = False

    async def slow_answer(query_id: str):
        nonlocal provider_closed, persisted
        try:
            yield encode_sse_event(
                answer_event(
                    "chunk",
                    query_id,
                    done=False,
                    chunk="partial",
                )
            )
            await asyncio.Event().wait()
            persisted = True
        finally:
            provider_closed = True

    api = FastAPI()
    api.add_middleware(
        RAGRequestReliabilityMiddleware,
        max_concurrent_queries=1,
        request_timeout_seconds=0.01,
    )

    @api.get("/query/stream")
    async def stream(request: Request) -> StreamingResponse:
        return StreamingResponse(
            slow_answer(request.state.request_id),
            media_type="text/event-stream",
        )

    with TestClient(api) as client:
        response = client.get("/query/stream")

    events = [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]

    assert response.status_code == 200
    assert provider_closed is True
    assert persisted is False
    assert events[0]["event"] == "chunk"
    assert events[0]["chunk"] == "partial"
    assert events[-1] == {
        "event": "error",
        "query_id": events[0]["query_id"],
        "done": True,
        "error": {
            "code": "request_timeout",
            "message": "请求处理超时，请稍后重试",
        },
    }

def test_query_deadline_returns_safe_504_and_cancels_work(
    isolated_runtime,
) -> None:
    from src.api.reliability import RAGRequestReliabilityMiddleware

    cancelled = False

    async def slow_query(scope, receive, send) -> None:
        nonlocal cancelled
        try:
            await asyncio.Event().wait()
        finally:
            cancelled = True

    async def run() -> list[dict]:
        messages: list[dict] = []

        async def send(message: dict) -> None:
            messages.append(message)

        middleware = RAGRequestReliabilityMiddleware(
            slow_query,
            max_concurrent_queries=1,
            request_timeout_seconds=0.01,
        )
        await middleware(_scope("/query"), _receive, send)
        return messages

    messages = asyncio.run(run())
    payload = json.loads(messages[-1]["body"])

    assert cancelled is True
    assert messages[0]["status"] == 504
    assert payload["detail"] == {
        "code": "request_timeout",
        "message": "请求处理超时，请稍后重试",
    }
    assert len(payload["query_id"]) == 12


def test_capacity_rejects_overlap_and_recovers_after_release(
    isolated_runtime,
) -> None:
    from src.api.reliability import RAGRequestReliabilityMiddleware

    first_started = asyncio.Event()
    release_first = asyncio.Event()

    async def controlled_app(scope, receive, send) -> None:
        first_started.set()
        await release_first.wait()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": b"ok",
                "more_body": False,
            }
        )

    async def invoke(middleware, path: str) -> list[dict]:
        messages: list[dict] = []

        async def send(message: dict) -> None:
            messages.append(message)

        await middleware(_scope(path), _receive, send)
        return messages

    async def run() -> tuple[list[dict], list[dict]]:
        middleware = RAGRequestReliabilityMiddleware(
            controlled_app,
            max_concurrent_queries=1,
            request_timeout_seconds=1,
        )
        first = asyncio.create_task(invoke(middleware, "/query"))
        await first_started.wait()

        rejected = await invoke(middleware, "/query")
        release_first.set()
        await first
        recovered = await invoke(middleware, "/query")
        return rejected, recovered

    rejected, recovered = asyncio.run(run())

    assert rejected[0]["status"] == 503
    assert json.loads(rejected[-1]["body"])["detail"]["code"] == (
        "query_capacity_exceeded"
    )
    assert recovered[0]["status"] == 200
    assert recovered[-1]["body"] == b"ok"


@pytest.mark.parametrize(
    ("reranker", "expected_warning"),
    [
        (
            SimpleNamespace(
                rerank=lambda query, candidates, top_k: (
                    time.sleep(0.05),
                    candidates,
                )[1]
            ),
            "rerank_timeout",
        ),
        (
            SimpleNamespace(
                rerank=lambda query, candidates, top_k: (_ for _ in ()).throw(
                    RuntimeError("reranker unavailable")
                )
            ),
            "rerank_unavailable",
        ),
    ],
)
def test_rerank_failure_degrades_to_fusion_order(
    isolated_runtime,
    monkeypatch: pytest.MonkeyPatch,
    reranker,
    expected_warning: str,
) -> None:
    from config.settings import settings
    from src.core.knowledge import KnowledgeSystem

    class Rewriter:
        async def rewrite(self, query: str) -> list[str]:
            return [query]

    retriever = SimpleNamespace(
        search=lambda queries, metadata_filter, mode, top_k: [
            {"chunk_id": "lower", "text": "a", "rrf_score": 0.1},
            {"chunk_id": "higher", "text": "b", "rrf_score": 0.9},
        ]
    )
    monkeypatch.setattr(settings, "rerank_timeout_seconds", 0.005)
    system = KnowledgeSystem(
        rewriter=Rewriter(),
        retriever=retriever,
        reranker=reranker,
    )

    result = asyncio.run(system.retrieve("question", top_k=1))

    assert [chunk["chunk_id"] for chunk in result.chunks] == ["higher"]
    assert result.chunks[0]["rerank_score"] == 0.9
    assert result.warnings == (expected_warning,)


def test_empty_recall_is_explicit_and_skips_reranker(
    isolated_runtime,
) -> None:
    from src.core.knowledge import KnowledgeSystem

    class Rewriter:
        async def rewrite(self, query: str) -> list[str]:
            return [query]

    reranker = SimpleNamespace(
        rerank=lambda *args, **kwargs: pytest.fail(
            "empty recall must not invoke reranker"
        )
    )
    system = KnowledgeSystem(
        rewriter=Rewriter(),
        retriever=SimpleNamespace(search=lambda *args, **kwargs: []),
        reranker=reranker,
    )

    result = asyncio.run(system.retrieve("missing"))

    assert result.candidates == []
    assert result.chunks == []
    assert result.warnings == ()


def test_all_generation_providers_failed_is_not_a_normal_answer(
    isolated_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from config.settings import settings
    from src.core.errors import GenerationUnavailableError
    from src.infra import fallback

    async def provider_failure(*args, **kwargs):
        raise RuntimeError("provider-secret")

    monkeypatch.setattr(fallback.llm_client, "chat_async", provider_failure)
    monkeypatch.setattr(settings, "ollama_base_url", None)

    with pytest.raises(GenerationUnavailableError):
        asyncio.run(
            fallback.chat_with_fallback_async(
                [{"role": "user", "content": "question"}]
            )
        )
