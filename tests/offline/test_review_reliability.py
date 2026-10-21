"""Permanent regressions for the 2026-10-20 reliability review."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncIterator
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def fixed_tokenizer(monkeypatch: pytest.MonkeyPatch) -> None:
    import tiktoken

    monkeypatch.setattr(tiktoken, "get_encoding", lambda _name: SimpleNamespace(
        encode=lambda text: list(text),
    ))


def _harness(tmp_path: Path, **limits: Any) -> Any:
    from src.agent.harness import AgentConfig, AgentHarness
    from src.agent.hooks import HookPipeline
    from src.agent.memory import MemoryConfig, MemoryManager
    from src.agent.tools import ToolRegistry
    from src.infra.session_store import SessionStore

    return AgentHarness(
        config=AgentConfig(verbose=False, **limits),
        memory=MemoryManager(
            tmp_path / "memory", config=MemoryConfig(compress_trigger_turns=100),
            store=SessionStore(str(tmp_path / "sessions.db")),
        ),
        tools=ToolRegistry(), hooks=HookPipeline(),
    )


def _answer_plan(monkeypatch: pytest.MonkeyPatch, harness: Any) -> None:
    from src.agent.harness import _FinalAnswerPlan
    from src.llm import llm_client

    async def plan(*_args: Any, **_kwargs: Any) -> Any:
        return _FinalAnswerPlan()

    async def stream(*_args: Any, **_kwargs: Any) -> AsyncIterator[str]:
        yield "complete answer"

    monkeypatch.setattr(harness, "_plan_async", plan)
    monkeypatch.setattr(llm_client, "chat_stream_async", stream)


def test_agent_reset_during_generation_discards_old_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.llm import llm_client

    harness = _harness(tmp_path)
    _answer_plan(monkeypatch, harness)
    harness.memory.add_turn("session", "user", "previous")
    harness.memory.add_turn("other", "user", "keep")

    async def stream(*_args: Any, **_kwargs: Any) -> AsyncIterator[str]:
        assert harness.reset_session("session")
        yield "old answer"

    monkeypatch.setattr(llm_client, "chat_stream_async", stream)
    result = asyncio.run(harness.execute("session", "old question"))
    assert result.error_code == "agent_session_reset"
    assert harness.memory.load_history("session") == []
    assert [turn.content for turn in harness.memory.load_history("other")] == ["keep"]

    _answer_plan(monkeypatch, harness)
    assert not asyncio.run(harness.execute("session", "new question")).error_code
    assert [turn.content for turn in harness.memory.load_history("session")] == [
        "new question", "complete answer",
    ]


def test_rag_reset_before_persist_discards_old_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.core import generation
    from src.core.generation import AnswerInput, AnswerService
    from src.infra.session_store import SessionStore

    store = SessionStore(str(tmp_path / "rag.db"))
    monkeypatch.setattr(generation, "session_store", store)
    store.append_exchange("session", "previous", "previous answer")
    store.append_exchange("other", "keep", "keep answer")
    answer_input = AnswerInput(
        query="old question", context="evidence", history=store.get_history("session"),
        session_id="session", valid_citation_refs=frozenset(),
    )
    assert store.clear("session")
    AnswerService.persist(answer_input, "old answer")
    assert store.get_history("session") == []
    assert store.get_history("other")[0]["content"] == "keep"


@pytest.mark.parametrize("streaming", [False, True])
def test_rag_epoch_captured_before_retrieval_and_reset_has_no_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, streaming: bool,
) -> None:
    import time

    from src.api.streaming import iter_answer_sse
    from src.core import generation
    from src.core.generation import AnswerInput, AnswerService, GeneratorNode
    from src.infra.session_store import SessionStore
    from src.orchestration import rag

    store = SessionStore(str(tmp_path / "rag.db"))
    monkeypatch.setattr(generation, "session_store", store)
    monkeypatch.setattr(rag, "session_store", store)
    store.append_exchange("session", "previous", "previous answer")
    shared: dict[str, Any] = {"query": "question", "session_id": "session"}
    asyncio.run(rag.KnowledgeRetrievalNode().prep_async(shared))
    assert store.clear("session")
    answer_input = AnswerInput.from_shared(shared)

    async def stream(*_args: Any) -> AsyncIterator[str]:
        yield "old answer"

    class Request:
        async def is_disconnected(self) -> bool:
            return False

    if streaming:
        service = AnswerService()
        monkeypatch.setattr(service, "stream", stream)

        async def consume() -> list[str]:
            return [event async for event in iter_answer_sse(
                request=Request(), service=service, answer_input=answer_input,
                sources=[], warnings=[], index_version="legacy",
                public_session_id="session", query_id="query", started_at=time.perf_counter(),
            )]

        events = asyncio.run(consume())
        payloads = [json.loads(event.removeprefix("data: ")) for event in events[1:]]
        assert payloads[-1]["event"] == "error"
        assert not any(payload["event"] == "done" for payload in payloads)
    else:
        with pytest.raises(RuntimeError, match="Session changed"):
            asyncio.run(GeneratorNode().post_async(shared, answer_input, "old answer"))
        assert "answer" not in shared
    assert store.get_history("session") == []
    fresh = AnswerInput.from_shared({"query": "fresh", "session_id": "session"})
    assert AnswerService.persist(fresh, "new answer")
    assert store.get_history("session")[0]["content"] == "fresh"


def test_memory_save_is_inside_agent_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.core.agent_runtime import AgentEventKind

    harness = _harness(tmp_path, timeout_seconds=0.05)
    _answer_plan(monkeypatch, harness)

    async def slow_save(*_args: Any, **_kwargs: Any) -> Any:
        await asyncio.Event().wait()

    monkeypatch.setattr(harness.memory, "add_turns_async", slow_save)

    async def exercise() -> None:
        task = asyncio.create_task(_collect(harness))
        try:
            done, _ = await asyncio.wait({task}, timeout=0.5)
            assert task in done, "memory persistence escaped the request deadline"
            events = await task
            assert events[-1].error_code == "agent_timeout"
            assert sum(event.kind in {AgentEventKind.DONE, AgentEventKind.ERROR} for event in events) == 1
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(exercise())
    assert harness.memory.load_history("session") == []


async def _collect(harness: Any) -> list[Any]:
    return [event async for event in harness.events("session", "question")]


@pytest.mark.parametrize("outcome", ["timeout", "cancel", "reset"])
def test_interrupted_compression_cannot_write_late_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str,
) -> None:
    harness = _harness(tmp_path, timeout_seconds=0.1 if outcome == "timeout" else 3)
    _answer_plan(monkeypatch, harness)
    memory = harness.memory
    memory.add_turn("session", "user", "previous")
    memory.config.compress_trigger_turns = 2
    memory.config.compress_keep_recent = 1
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def compress(*_args: Any) -> str:
        started.set()
        assert release.wait(3)
        return "summary"

    original_save = memory.add_turns

    def save(*args: Any, **kwargs: Any) -> Any:
        try:
            return original_save(*args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(memory, "_llm_compress", compress)
    monkeypatch.setattr(memory, "add_turns", save)

    async def exercise() -> None:
        task = asyncio.create_task(harness.execute("session", "question"))
        try:
            assert await asyncio.to_thread(started.wait, 1)
            if outcome == "timeout":
                done, _ = await asyncio.wait({task}, timeout=0.5)
                assert task in done, "compression escaped the request deadline"
                assert (await task).error_code == "agent_timeout"
            elif outcome == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                harness.reset_session("session")
                release.set()
                assert (await task).error_code == "agent_session_reset"
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            assert await asyncio.to_thread(finished.wait, 1)

    asyncio.run(exercise())
    assert [turn.content for turn in memory.load_history("session")] == (
        [] if outcome == "reset" else ["previous"]
    )


@pytest.mark.parametrize("replace", [False, True])
def test_expired_write_guard_rolls_back_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replace: bool,
) -> None:
    from src.infra.session_store import SessionStore, SessionTurn

    store = SessionStore(str(tmp_path / "guard.db"))
    store.append_exchange("session", "old", "old answer")
    snapshot = store.read_snapshot("session")
    permitted = True
    insert = store._insert_turns

    def revoke(*args: Any) -> None:
        nonlocal permitted
        insert(*args)
        permitted = False

    monkeypatch.setattr(store, "_insert_turns", revoke)
    pending = [SessionTurn(role="assistant", content="late")]
    if replace:
        saved = store.replace_history_if_revision(
            "session", snapshot.revision, pending, can_commit=lambda: permitted,
        )
    else:
        saved = store.append_turns_if_epoch(
            "session", snapshot.revision[0], pending, can_commit=lambda: permitted,
        )
    assert not saved
    assert store.read_snapshot("session") == snapshot


@pytest.mark.parametrize("mode", ["json", "native"])
@pytest.mark.parametrize("step_limit", [False, True])
def test_final_answer_has_its_own_output_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, step_limit: bool,
) -> None:
    from src.agent.tools import SafetyLevel, ToolDef, ToolResult
    from src.llm import llm_client
    from src.llm.client import NativeChatResponse, NativeToolCall

    harness = _harness(
        tmp_path, planner_mode=mode, planner_max_tokens=64, final_max_tokens=512,
        max_iterations=1,
    )
    harness.tools.register(ToolDef(
        "lookup", "test lookup", [], SafetyLevel.WHITELIST,
        execute_fn=lambda _params: ToolResult(success=True, data="evidence"),
    ))
    planner_limits: list[int] = []
    final_limits: list[int] = []

    async def json_plan(*_args: Any, **kwargs: Any) -> str:
        planner_limits.append(kwargs["max_tokens"])
        return ('{"action":"tool_call","tool_name":"lookup","tool_params":{}}'
                if step_limit else '{"action":"final_answer"}')

    async def native_plan(*_args: Any, **kwargs: Any) -> NativeChatResponse:
        planner_limits.append(kwargs["max_tokens"])
        return (NativeChatResponse("", (NativeToolCall("call", "lookup", "{}"),))
                if step_limit else NativeChatResponse("READY_TO_ANSWER"))

    async def stream(*_args: Any, **kwargs: Any) -> AsyncIterator[str]:
        final_limits.append(kwargs["max_tokens"])
        yield "complete answer"

    monkeypatch.setattr(llm_client, "chat_async", json_plan)
    monkeypatch.setattr(llm_client, "chat_with_tools_async", native_plan)
    monkeypatch.setattr(llm_client, "chat_stream_async", stream)
    result = asyncio.run(harness.execute("session", "question"))
    assert not result.error_code
    assert planner_limits == [64]
    assert final_limits == [512]


@pytest.mark.parametrize("payload", [
    "queries: null", "queries: scalar", "queries: [1, null]", "queries: []",
    "queries: ['   ']", "true", "queries: [unterminated",
    "queries: [a, b, c, d, e, f]", "queries: ['" + "x" * 2001 + "']",
    "queries: ['" + "x" * 20001 + "']", "queries: {}",
])
def test_invalid_rewrite_falls_back_to_original(
    monkeypatch: pytest.MonkeyPatch, payload: str,
) -> None:
    from src.core.retrieval import QueryRewriterNode
    from src.llm import llm_client

    async def chat(*_args: Any, **_kwargs: Any) -> str:
        return "```yaml\n" + payload + "\n```"

    monkeypatch.setattr(llm_client, "chat_async", chat)
    assert asyncio.run(QueryRewriterNode().rewrite("question")) == ["question"]


@pytest.mark.parametrize("payload", [
    "queries: [alternative, question, ' alternative ']", "[question, alternative]",
])
def test_valid_rewrite_is_bounded_and_keeps_original(
    monkeypatch: pytest.MonkeyPatch, payload: str,
) -> None:
    from src.core.retrieval import QueryRewriterNode
    from src.llm import llm_client

    async def chat(*_args: Any, **_kwargs: Any) -> str:
        return "```yaml\n" + payload + "\n```"

    monkeypatch.setattr(llm_client, "chat_async", chat)
    assert asyncio.run(QueryRewriterNode().rewrite("question")) == ["question", "alternative"]


@pytest.mark.parametrize(("reason", "content"), [
    ("length", "incomplete"), ("content_filter", "filtered"), (None, "unconfirmed"),
    ("stop", ""), ("stop", "   "),
])
def test_ollama_rejects_incomplete_answer(
    monkeypatch: pytest.MonkeyPatch, reason: str | None, content: str,
) -> None:
    from src.infra import fallback

    response = SimpleNamespace(choices=[SimpleNamespace(
        finish_reason=reason,
        message=SimpleNamespace(content=content, tool_calls=None),
    )])
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=lambda **_kwargs: response,
    )))
    monkeypatch.setattr(fallback, "OpenAI", lambda **_kwargs: nullcontext(client))
    with pytest.raises(RuntimeError, match="complete response"):
        fallback._chat_with_ollama([{"role": "user", "content": "question"}], 0.3)


@pytest.mark.parametrize("complete", [False, True])
def test_fallback_budget_and_final_history_follow_completion_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, complete: bool,
) -> None:
    from config.settings import settings
    from src.core import generation
    from src.core.errors import GenerationUnavailableError
    from src.core.generation import AnswerInput, AnswerService
    from src.infra import fallback
    from src.infra.session_store import SessionStore

    store = SessionStore(str(tmp_path / "fallback.db"))
    monkeypatch.setattr(generation, "session_store", store)
    monkeypatch.setattr(settings, "ollama_base_url", "http://127.0.0.1:9/v1")
    monkeypatch.setattr(AnswerService, "_config", staticmethod(lambda: {
        "model": "test", "temperature": 0.3, "max_tokens": 512,
    }))
    captured: dict[str, Any] = {}
    closed: list[bool] = []

    def create(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="stop" if complete else "length",
            message=SimpleNamespace(content="answer", tool_calls=None),
        )])

    class Client:
        chat = SimpleNamespace(completions=SimpleNamespace(create=create))

        def __enter__(self) -> Client:
            return self

        def __exit__(self, *_args: Any) -> None:
            closed.append(True)

    async def fail_primary(*_args: Any, **_kwargs: Any) -> str:
        raise RuntimeError("synthetic-secret")

    monkeypatch.setattr(fallback.llm_client, "chat_async", fail_primary)
    monkeypatch.setattr(fallback, "OpenAI", lambda **_kwargs: Client())
    answer_input = AnswerInput(
        query="question", context="evidence", history=[], session_id="session",
        valid_citation_refs=frozenset(),
    )

    async def exercise() -> None:
        answer = await AnswerService().generate(answer_input)
        AnswerService.persist(answer_input, answer)

    if complete:
        asyncio.run(exercise())
        assert store.history_count("session") == 2
    else:
        with pytest.raises(GenerationUnavailableError, match="all answer-generation"):
            asyncio.run(exercise())
        assert store.history_count("session") == 0
    assert captured["max_tokens"] == 512
    assert closed == [True]


def test_concurrent_identical_tool_calls_execute_once() -> None:
    from src.agent.tool_approval import ToolApprovalDecision
    from src.agent.tools import SafetyLevel, ToolDef, ToolRegistry, ToolResult

    count = 0

    class Approver:
        async def authorize(self, _session_id: str, **kwargs: Any) -> ToolApprovalDecision:
            await asyncio.sleep(0)
            return ToolApprovalDecision("approved", kwargs["params"])

    async def execute(_params: dict) -> ToolResult:
        nonlocal count
        count += 1
        await asyncio.sleep(0)
        return ToolResult(success=True)

    registry = ToolRegistry(approval_manager=Approver())
    registry.register(ToolDef(
        "effect", "test", [], SafetyLevel.GRAYLIST,
        execute_fn=lambda _params: ToolResult(success=True), execute_async_fn=execute,
    ))

    async def exercise() -> list[ToolResult]:
        return await asyncio.gather(*(
            registry.execute_async("effect", {}, "session") for _ in range(2)
        ))

    results = asyncio.run(exercise())
    assert count == 1
    assert sum(result.success for result in results) == 1
    assert [result.error_code for result in results if not result.success] == ["duplicate_tool_call"]
