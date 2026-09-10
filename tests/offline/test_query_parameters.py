"""Regression tests for public retrieval parameter wiring and validation."""

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def reset_http_modules() -> None:
    for name in ("app", "src.infra.tracer"):
        sys.modules.pop(name, None)
    yield
    for name in ("app", "src.infra.tracer"):
        sys.modules.pop(name, None)


@pytest.fixture(autouse=True)
def fixed_tokenizer(monkeypatch: pytest.MonkeyPatch) -> None:
    import tiktoken

    monkeypatch.setattr(
        tiktoken,
        "get_encoding",
        lambda name: SimpleNamespace(encode=lambda text: list(text)),
    )


def test_post_query_forwards_all_retrieval_parameters(
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app as api

    calls: list[dict] = []

    class QueryFlow:
        async def run_async(self, shared: dict) -> None:
            calls.append(shared.copy())
            shared.update(answer="answer", sources=[])

    monkeypatch.setattr(api, "get_online_flow", QueryFlow)
    client = TestClient(api.app)
    try:
        response = client.post(
            "/query",
            json={
                "query": "probe",
                "top_k": 7,
                "filter": {"category": {"$eq": "public"}},
                "retrieval_mode": "bm25_only",
            },
        )
    finally:
        client.close()

    assert response.status_code == 200
    assert {
        key: calls[0][key]
        for key in ("query", "top_k", "filter", "retrieval_mode")
    } == {
        "query": "probe",
        "top_k": 7,
        "filter": {"category": {"$eq": "public"}},
        "retrieval_mode": "bm25_only",
    }


def test_stream_query_forwards_all_retrieval_parameters(
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app as api

    calls: list[dict] = []

    class RetrievalFlow:
        async def run_async(self, shared: dict) -> None:
            calls.append(shared.copy())
            shared.update(context="context", sources=[])

    async def stream_answer(*args, **kwargs):
        yield "answer"

    monkeypatch.setattr(api, "get_retrieval_flow", RetrievalFlow)
    from src.core import generation

    monkeypatch.setattr(generation.llm_client, "chat_stream_async", stream_answer)

    client = TestClient(api.app)
    try:
        response = client.get(
            "/query/stream",
            params={
                "query": "probe",
                "top_k": 1,
                "filter": json.dumps({"category": "public"}),
                "retrieval_mode": "vector_only",
            },
        )
    finally:
        client.close()

    assert response.status_code == 200
    assert {
        key: calls[0][key]
        for key in ("query", "top_k", "filter", "retrieval_mode")
    } == {
        "query": "probe",
        "top_k": 1,
        "filter": {"category": "public"},
        "retrieval_mode": "vector_only",
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"query": "probe", "top_k": 0},
        {"query": "probe", "top_k": 21},
        {"query": "probe", "top_k": True},
        {"query": "probe", "retrieval_mode": "semantic"},
        {"query": "probe", "filter": {}},
        {"query": "probe", "filter": {"category": []}},
    ],
)
def test_post_query_rejects_invalid_retrieval_parameters(
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: dict,
) -> None:
    import app as api

    class UnexpectedFlow:
        async def run_async(self, shared: dict) -> None:
            raise AssertionError("invalid request reached retrieval")

    monkeypatch.setattr(api, "get_online_flow", UnexpectedFlow)
    client = TestClient(api.app)
    try:
        response = client.post("/query", json=payload)
    finally:
        client.close()

    assert response.status_code == 422


@pytest.mark.parametrize(
    "params",
    [
        {"query": "probe", "top_k": 0},
        {"query": "probe", "top_k": 21},
        {"query": "probe", "retrieval_mode": "semantic"},
        {"query": "probe", "filter": "not-json"},
        {"query": "probe", "filter": json.dumps({"category": []})},
    ],
)
def test_stream_query_rejects_invalid_retrieval_parameters(
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
    params: dict,
) -> None:
    import app as api

    class UnexpectedFlow:
        async def run_async(self, shared: dict) -> None:
            raise AssertionError("invalid request reached retrieval")

    monkeypatch.setattr(api, "get_retrieval_flow", UnexpectedFlow)
    client = TestClient(api.app)
    try:
        response = client.get("/query/stream", params=params)
    finally:
        client.close()

    assert response.status_code == 422


@pytest.mark.parametrize(
    "kwargs",
    [
        {"top_k": 0},
        {"top_k": 21},
        {"top_k": True},
        {"mode": "semantic"},
        {"metadata_filter": {}},
        {"metadata_filter": {"category": []}},
    ],
)
def test_knowledge_system_rejects_invalid_parameters_before_retrieval(
    isolated_runtime: Path,
    kwargs: dict,
) -> None:
    from src.core.knowledge import KnowledgeSystem

    class UnexpectedDependency:
        def __getattr__(self, name: str):
            raise AssertionError(f"invalid request reached {name}")

    dependency = UnexpectedDependency()
    system = KnowledgeSystem(
        rewriter=dependency,
        retriever=dependency,
        reranker=dependency,
    )

    with pytest.raises(ValueError):
        asyncio.run(system.retrieve("probe", **kwargs))


def test_sync_agent_rejects_out_of_range_top_k(
    isolated_runtime: Path,
) -> None:
    from src.agent.tools import ToolRegistry, _create_search_kb_tool

    registry = ToolRegistry(dedup_window=0)
    registry.register(_create_search_kb_tool())

    result = registry.execute(
        "search_knowledge_base",
        {"query": "probe", "top_k": 21},
        "session",
    )

    assert not result.success
    assert result.error_code == "invalid_tool_parameters"
    assert "at most 20" in result.error


def test_async_agent_rejects_invalid_mode(
    isolated_runtime: Path,
) -> None:
    from src.agent.tools import ToolRegistry, _create_search_kb_tool

    registry = ToolRegistry(dedup_window=0)
    registry.register(_create_search_kb_tool())
    result = asyncio.run(
        registry.execute_async(
            "search_knowledge_base",
            {"query": "probe", "retrieval_mode": "semantic"},
            "session",
        )
    )

    assert not result.success
    assert result.error_code == "invalid_tool_parameters"
    assert "must be one of" in result.error
