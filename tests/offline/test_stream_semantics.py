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
                warnings=["bm25_version_mismatch"],
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
    assert completed["warnings"] == ["bm25_version_mismatch"]
    assert completed["index_version"] == "legacy"
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
                warnings=[],
                index_version="legacy",
                public_session_id="public-session",
                query_id="query-id",
                started_at=time.perf_counter(),
            )
        ]

    events = asyncio.run(consume())

    assert events == ["retry: 3000\n\n"]
    assert provider_closed is True
    assert persisted is False


def test_agent_http_sse_and_memory_share_the_complete_streamed_answer(
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app as api
    from src.agent.harness import AgentConfig, AgentHarness
    from src.agent.hooks import HookPipeline
    from src.agent.memory import MemoryConfig, MemoryManager
    from src.agent.tools import ToolRegistry
    from src.infra.session_store import SessionStore
    from src.llm import llm_client

    memory = MemoryManager(
        str(isolated_runtime / "agent-stream-memory"),
        config=MemoryConfig(compress_trigger_turns=100),
        store=SessionStore(str(isolated_runtime / "agent-stream.db")),
    )
    harness = AgentHarness(
        config=AgentConfig(verbose=False),
        memory=memory,
        tools=ToolRegistry(dedup_window=0),
        hooks=HookPipeline(),
    )
    storage_ids: list[str] = []

    class Runtime:
        async def execute(self, session_id: str, message: str):
            storage_ids.append(session_id)
            return await harness.execute(session_id, message)

        def events(self, session_id: str, message: str):
            storage_ids.append(session_id)
            return harness.events(session_id, message)

        def reset_session(self, session_id: str) -> bool:
            return harness.reset_session(session_id)

    async def chat_async(*_args: object, **_kwargs: object) -> str:
        return json.dumps({"action": "final_answer"})

    async def chat_stream_async(*_args: object, **_kwargs: object):
        yield "shared"
        yield " answer"

    monkeypatch.setattr(api, "agent_runtime", Runtime())
    monkeypatch.setattr(llm_client, "chat_async", chat_async)
    monkeypatch.setattr(llm_client, "chat_stream_async", chat_stream_async)

    client = TestClient(api.app)
    try:
        ordinary = client.post(
            "/agent/chat",
            json={"message": "question", "session_id": "ordinary"},
        )
        streamed = client.post(
            "/agent/chat/stream",
            json={"message": "question", "session_id": "streamed"},
        )
    finally:
        client.close()

    stream_events = _data_events(streamed.text)
    chunks = [event["chunk"] for event in stream_events if "chunk" in event]
    completed = stream_events[-1]

    assert ordinary.status_code == 200
    assert streamed.status_code == 200
    assert ordinary.json()["answer"] == "shared answer"
    assert "".join(chunks) == completed["answer"] == "shared answer"
    assert completed["done"] is True
    assert completed["session_id"] == "streamed"
    assert len(storage_ids) == 2
    assert all(storage_id not in {"ordinary", "streamed"} for storage_id in storage_ids)
    assert [
        memory.load_history(storage_id)[-1].content for storage_id in storage_ids
    ] == ["shared answer", "shared answer"]


def test_agent_disconnect_closes_runtime_stream_without_terminal_event() -> None:
    from src.api.streaming import iter_agent_sse
    from src.core.agent_runtime import AgentEvent, AgentEventKind

    runtime_closed = False

    class Runtime:
        async def events(self, session_id: str, message: str):
            nonlocal runtime_closed
            try:
                yield AgentEvent(AgentEventKind.CHUNK, chunk="partial")
                await asyncio.Event().wait()
            finally:
                runtime_closed = True

    class DisconnectedRequest:
        async def is_disconnected(self) -> bool:
            return True

    async def consume() -> list[str]:
        return [
            event
            async for event in iter_agent_sse(
                request=DisconnectedRequest(),
                runtime=Runtime(),
                storage_session_id="private-session",
                public_session_id="public-session",
                message="question",
            )
        ]

    events = asyncio.run(consume())

    assert events == ["retry: 3000\n\n"]
    assert runtime_closed is True
