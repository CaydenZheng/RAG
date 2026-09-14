"""Regression tests for public retrieval parameter wiring and validation."""

import asyncio
import json
import sys
from collections.abc import AsyncIterator
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
            shared.update(
                answer="answer",
                sources=[],
                warnings=["bm25_version_mismatch"],
            )

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
    assert response.json()["warnings"] == ["bm25_version_mismatch"]


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


def test_agent_stream_uses_post_json_body(
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app as api
    from src.core.agent_runtime import AgentEvent, AgentEventKind, AgentResponse

    calls: list[tuple[str, str]] = []

    class AgentRuntime:
        async def events(
            self, session_id: str, message: str
        ) -> AsyncIterator[AgentEvent]:
            calls.append((session_id, message))
            yield AgentEvent(
                AgentEventKind.DONE,
                response=AgentResponse(session_id, "answer"),
            )

    monkeypatch.setattr(api, "agent_runtime", AgentRuntime())
    client = TestClient(api.app)
    try:
        response = client.post(
            "/agent/chat/stream",
            json={"message": "probe", "session_id": "public-session"},
        )
        get_response = client.get(
            "/agent/chat/stream",
            params={"message": "probe", "session_id": "public-session"},
        )
    finally:
        client.close()

    assert response.status_code == 200
    assert calls[0][1] == "probe"
    assert "\"done\": true" in response.text
    assert get_response.status_code == 405


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


@pytest.mark.parametrize("value", ["", "x" * 2001])
@pytest.mark.parametrize(
    ("method", "path", "field"),
    [
        ("POST", "/query", "query"),
        ("GET", "/query/stream", "query"),
        ("POST", "/agent/chat", "message"),
        ("POST", "/agent/chat/stream", "message"),
    ],
)
def test_public_query_and_agent_inputs_reject_invalid_lengths_before_runtime(
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
    value: str,
    method: str,
    path: str,
    field: str,
) -> None:
    import app as api

    class UnexpectedFlow:
        async def run_async(self, shared: dict) -> None:
            raise AssertionError("invalid request reached retrieval")

    class UnexpectedAgentRuntime:
        async def execute(self, session_id: str, message: str) -> None:
            raise AssertionError("invalid request reached Agent runtime")

        async def events(self, session_id: str, message: str) -> AsyncIterator[None]:
            raise AssertionError("invalid request reached Agent runtime")
            yield

    monkeypatch.setattr(api, "get_online_flow", UnexpectedFlow)
    monkeypatch.setattr(api, "get_retrieval_flow", UnexpectedFlow)
    monkeypatch.setattr(api, "agent_runtime", UnexpectedAgentRuntime())
    client = TestClient(api.app, raise_server_exceptions=False)
    try:
        request_kwargs: dict[str, dict[str, str]] = {
            "json" if method == "POST" else "params": {field: value}
        }
        response = client.request(method, path, **request_kwargs)
    finally:
        client.close()

    assert response.status_code == 422


@pytest.mark.parametrize("value", ["x", "x" * 2000])
@pytest.mark.parametrize(
    ("method", "path", "field"),
    [
        ("POST", "/query", "query"),
        ("GET", "/query/stream", "query"),
        ("POST", "/agent/chat", "message"),
        ("POST", "/agent/chat/stream", "message"),
    ],
)
def test_public_query_and_agent_inputs_accept_length_boundaries(
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
    value: str,
    method: str,
    path: str,
    field: str,
) -> None:
    import app as api
    from src.core import generation
    from src.core.agent_runtime import AgentEvent, AgentEventKind, AgentResponse

    class AcceptingFlow:
        async def run_async(self, shared: dict) -> None:
            shared.update(answer="answer", context="context", sources=[])

    class AcceptingAgentRuntime:
        async def execute(self, session_id: str, message: str) -> AgentResponse:
            return AgentResponse(session_id=session_id, answer="answer")

        async def events(
            self, session_id: str, message: str
        ) -> AsyncIterator[AgentEvent]:
            response = await self.execute(session_id, message)
            yield AgentEvent(AgentEventKind.DONE, response=response)

    async def stream_answer(*args: object, **kwargs: object) -> AsyncIterator[str]:
        yield "answer"

    monkeypatch.setattr(api, "get_online_flow", AcceptingFlow)
    monkeypatch.setattr(api, "get_retrieval_flow", AcceptingFlow)
    monkeypatch.setattr(api, "agent_runtime", AcceptingAgentRuntime())
    monkeypatch.setattr(generation.llm_client, "chat_stream_async", stream_answer)
    client = TestClient(api.app)
    try:
        request_kwargs: dict[str, dict[str, str]] = {
            "json" if method == "POST" else "params": {field: value}
        }
        response = client.request(method, path, **request_kwargs)
    finally:
        client.close()

    assert response.status_code == 200
