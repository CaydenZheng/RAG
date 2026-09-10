"""Regression coverage for request tracing, fault location, and redaction."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def _read_record(path: Path) -> dict:
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    return json.loads(lines[0])


def test_trace_redacts_content_but_keeps_operational_metrics(
    isolated_runtime: Path,
) -> None:
    from src.infra.tracer import TraceLogger

    trace_path = isolated_runtime / "traces.jsonl"
    local = TraceLogger(trace_path)
    trace = local.start_trace(
        "request-123",
        "/query",
        trace_id="trace-456",
    )
    token = local.bind(trace)
    local.add_span(
        None,
        "answer_generation",
        12.345,
        model="safe-model",
        cache_hit=True,
        total_tokens=17,
        query="private question",
        session_id="private session",
        api_key="sk-super-secret-value",
        tool_output="complete private tool response",
    )
    record = local.finish_trace(
        trace,
        answer="private answer",
        response_chars=42,
    )
    local.reset(token)

    persisted = _read_record(trace_path)
    assert persisted == record
    assert persisted["request_id"] == "request-123"
    assert persisted["trace_id"] == "trace-456"
    assert persisted["operation"] == "/query"
    assert persisted["spans"][0]["latency_ms"] == 12.35
    attributes = persisted["spans"][0]["attributes"]
    assert attributes["model"] == "safe-model"
    assert attributes["cache_hit"] is True
    assert attributes["total_tokens"] == 17
    assert attributes["query"] == "[REDACTED]"
    assert attributes["session_id"] == "[REDACTED]"
    assert attributes["api_key"] == "[REDACTED]"
    assert attributes["tool_output"] == "[REDACTED]"
    assert persisted["metrics"]["answer"] == "[REDACTED]"
    assert persisted["metrics"]["response_chars"] == 42

    encoded = trace_path.read_text(encoding="utf-8")
    for secret in (
        "private question",
        "private session",
        "sk-super-secret-value",
        "complete private tool response",
        "private answer",
    ):
        assert secret not in encoded


def test_request_middleware_returns_ids_and_records_failure_stage(
    monkeypatch: pytest.MonkeyPatch,
    isolated_runtime: Path,
) -> None:
    from src.api import observability
    from src.infra.tracer import TraceLogger

    trace_path = isolated_runtime / "request-trace.jsonl"
    local = TraceLogger(trace_path)
    monkeypatch.setattr(observability, "tracer", local)

    async def failing_endpoint(scope, receive, send) -> None:
        local.add_span(
            None,
            "candidate_retrieval",
            3.0,
            status="error",
            error_code="retrieval_failed",
        )
        local.record_error("retrieval_failed")
        await send(
            {
                "type": "http.response.start",
                "status": 503,
                "headers": [],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": b"{}",
                "more_body": False,
            }
        )

    middleware = observability.RequestTracingMiddleware(failing_endpoint)
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        sent.append(message)

    asyncio.run(
        middleware(
            {"type": "http", "path": "/query", "state": {}},
            receive,
            send,
        )
    )

    response_headers = dict(sent[0]["headers"])
    assert len(response_headers[b"x-request-id"]) == 12
    assert len(response_headers[b"x-trace-id"]) == 32

    record = _read_record(trace_path)
    assert record["request_id"] == response_headers[b"x-request-id"].decode()
    assert record["trace_id"] == response_headers[b"x-trace-id"].decode()
    assert record["status"] == "error"
    assert record["error_code"] == "retrieval_failed"
    assert record["spans"][0]["name"] == "candidate_retrieval"
    assert record["spans"][0]["error_code"] == "retrieval_failed"


def test_retrieval_failure_identifies_the_failed_stage(
    monkeypatch: pytest.MonkeyPatch,
    isolated_runtime: Path,
) -> None:
    from src.core import knowledge as knowledge_module
    from src.core.knowledge import KnowledgeSystem
    from src.infra.tracer import TraceLogger

    class Rewriter:
        async def rewrite(self, query: str) -> list[str]:
            return [query]

    class BrokenRetriever:
        def search(self, queries, metadata_filter, mode, top_k):
            raise RuntimeError("sk-secret-must-not-be-recorded")

    class UnusedReranker:
        def rerank(self, query, candidates, top_k):
            raise AssertionError("reranker should not run")

    trace_path = isolated_runtime / "retrieval-trace.jsonl"
    local = TraceLogger(trace_path)
    monkeypatch.setattr(knowledge_module, "tracer", local)
    trace = local.start_trace("request-id", "/query", trace_id="trace-id")
    token = local.bind(trace)
    system = KnowledgeSystem(Rewriter(), BrokenRetriever(), UnusedReranker())

    with pytest.raises(RuntimeError, match="must-not-be-recorded"):
        asyncio.run(system.retrieve("private query"))

    local.record_error("retrieval_failed")
    local.finish_trace(trace)
    local.reset(token)
    record = _read_record(trace_path)
    assert [span["name"] for span in record["spans"]] == [
        "query_rewrite",
        "candidate_retrieval",
    ]
    failed = record["spans"][-1]
    assert failed["status"] == "error"
    assert failed["error_code"] == "retrieval_failed"
    assert "sk-secret-must-not-be-recorded" not in trace_path.read_text(
        encoding="utf-8"
    )


def test_llm_span_records_cache_hit_and_reported_usage(
    monkeypatch: pytest.MonkeyPatch,
    isolated_runtime: Path,
) -> None:
    from src.infra.tracer import TraceLogger
    from src.llm import cache as cache_module
    from src.llm import llm_client

    trace_path = isolated_runtime / "llm-trace.jsonl"
    local = TraceLogger(trace_path)
    trace = local.start_trace("request-id", "/query", trace_id="trace-id")
    token = local.bind(trace)

    from src.llm import client as client_module

    monkeypatch.setattr(client_module, "tracer", local)

    usage = SimpleNamespace(
        prompt_tokens=5,
        completion_tokens=3,
        total_tokens=8,
    )
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(message=SimpleNamespace(content="generated answer"))
        ],
        usage=usage,
    )

    class Completions:
        async def create(self, **kwargs):
            return response

    fake_async_client = SimpleNamespace(
        chat=SimpleNamespace(completions=Completions())
    )
    monkeypatch.setattr(llm_client, "_async_chat_client", fake_async_client)

    with local.stage("answer_generation"):
        generated = asyncio.run(
            llm_client.chat_async(
                [{"role": "user", "content": "private"}],
                skip_cache=True,
            )
        )
    assert generated == "generated answer"

    monkeypatch.setattr(cache_module.llm_cache, "get", lambda *args, **kwargs: "cached")
    with local.stage("agent_planning"):
        cached = asyncio.run(
            llm_client.chat_async(
                [{"role": "user", "content": "private"}],
            )
        )
    assert cached == "cached"

    local.finish_trace(trace)
    local.reset(token)
    record = _read_record(trace_path)
    generated_span, cached_span = record["spans"]
    assert generated_span["name"] == "answer_generation"
    assert generated_span["attributes"]["cache_hit"] is False
    assert generated_span["attributes"]["prompt_tokens"] == 5
    assert generated_span["attributes"]["completion_tokens"] == 3
    assert generated_span["attributes"]["total_tokens"] == 8
    assert generated_span["attributes"]["usage_reported"] is True
    assert cached_span["name"] == "agent_planning"
    assert cached_span["attributes"]["cache_hit"] is True
    assert cached_span["attributes"]["total_tokens"] == 0

def test_agent_event_and_audit_logs_are_redacted(
    monkeypatch: pytest.MonkeyPatch,
    isolated_runtime: Path,
) -> None:
    from src.agent import hooks
    from src.infra.tracer import TraceLogger

    local = TraceLogger(isolated_runtime / "unused-trace.jsonl")
    monkeypatch.setattr(hooks, "tracer", local)
    trace = local.start_trace("request-id", "/agent/chat", trace_id="trace-id")
    token = local.bind(trace)
    context = hooks.HookContext(
        event=hooks.HookEvent.PRE_TOOL_USE,
        session_id="private-session",
        data={
            "user_message": "private question",
            "tool_name": "search_web",
            "tool_params": {"query": "private search", "max_results": 3},
        },
    )

    hooks.create_logging_hook(str(isolated_runtime))(context)
    hooks.create_audit_hook(str(isolated_runtime))(context)
    local.reset(token)

    event = _read_record(isolated_runtime / "agent_events.jsonl")
    audit = _read_record(isolated_runtime / "audit.jsonl")
    assert event["request_id"] == "request-id"
    assert event["trace_id"] == "trace-id"
    assert event["data"]["user_message"] == "[REDACTED]"
    assert event["data"]["tool_params"] == "[REDACTED]"
    assert audit["request_id"] == "request-id"
    assert audit["trace_id"] == "trace-id"
    assert audit["parameter_names"] == ["max_results", "query"]

    encoded = (
        (isolated_runtime / "agent_events.jsonl").read_text(encoding="utf-8")
        + (isolated_runtime / "audit.jsonl").read_text(encoding="utf-8")
    )
    for secret in ("private-session", "private question", "private search"):
        assert secret not in encoded

@pytest.mark.parametrize(
    ("response_status", "expected_error"),
    [(422, "request_validation_failed"), (404, "request_rejected")],
)
def test_request_middleware_classifies_client_errors(
    monkeypatch: pytest.MonkeyPatch,
    isolated_runtime: Path,
    response_status: int,
    expected_error: str,
) -> None:
    from src.api import observability
    from src.infra.tracer import TraceLogger

    trace_path = isolated_runtime / "client-error.jsonl"
    local = TraceLogger(trace_path)
    monkeypatch.setattr(observability, "tracer", local)

    async def endpoint(scope, receive, send) -> None:
        await send({"type": "http.response.start", "status": response_status, "headers": []})
        await send({"type": "http.response.body", "body": b"{}", "more_body": False})

    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        sent.append(message)

    asyncio.run(
        observability.RequestTracingMiddleware(endpoint)(
            {"type": "http", "path": "/query", "state": {}},
            receive,
            send,
        )
    )
    assert b"x-trace-id" in dict(sent[0]["headers"])
    record = _read_record(trace_path)
    assert record["status"] == "error"
    assert record["error_code"] == expected_error


def test_unhandled_error_keeps_trace_headers_and_safe_body(
    monkeypatch: pytest.MonkeyPatch,
    isolated_runtime: Path,
) -> None:
    from src.api import observability
    from src.infra.tracer import TraceLogger

    trace_path = isolated_runtime / "server-error.jsonl"
    local = TraceLogger(trace_path)
    monkeypatch.setattr(observability, "tracer", local)

    async def endpoint(scope, receive, send) -> None:
        raise RuntimeError("sk-private-provider-error")

    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        sent.append(message)

    asyncio.run(
        observability.RequestTracingMiddleware(endpoint)(
            {"type": "http", "path": "/query", "state": {}},
            receive,
            send,
        )
    )
    headers = dict(sent[0]["headers"])
    assert sent[0]["status"] == 500
    assert b"x-request-id" in headers
    assert b"x-trace-id" in headers
    body = json.loads(sent[1]["body"])
    assert body["detail"]["code"] == "internal_server_error"
    record = _read_record(trace_path)
    assert record["error_code"] == "unhandled_request_error"
    assert "sk-private-provider-error" not in trace_path.read_text(encoding="utf-8")


def test_query_rewrite_failure_records_its_stage(
    monkeypatch: pytest.MonkeyPatch,
    isolated_runtime: Path,
) -> None:
    from src.core import knowledge as knowledge_module
    from src.core.knowledge import KnowledgeSystem
    from src.infra.tracer import TraceLogger

    class BrokenRewriter:
        async def rewrite(self, query: str) -> list[str]:
            raise RuntimeError("provider failed")

    class Unused:
        def search(self, *args):
            raise AssertionError("retrieval should not run")

        def rerank(self, *args):
            raise AssertionError("reranker should not run")

    local = TraceLogger(isolated_runtime / "rewrite-error.jsonl")
    monkeypatch.setattr(knowledge_module, "tracer", local)
    trace = local.start_trace("request-id", "/query", trace_id="trace-id")
    token = local.bind(trace)
    with pytest.raises(RuntimeError, match="provider failed"):
        asyncio.run(
            KnowledgeSystem(BrokenRewriter(), Unused(), Unused()).retrieve(
                "private query"
            )
        )
    local.reset(token)
    assert trace["spans"][0]["name"] == "query_rewrite"
    assert trace["spans"][0]["status"] == "error"
    assert trace["spans"][0]["error_code"] == "query_rewrite_failed"
