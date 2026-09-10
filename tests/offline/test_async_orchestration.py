"""Regression coverage for callers of PocketFlow async pipelines."""

import asyncio
import json
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


def test_query_endpoint_awaits_flow_failure(
    monkeypatch: pytest.MonkeyPatch, isolated_runtime: Path
) -> None:
    import app as api

    class FailingFlow:
        async def run_async(self, shared: dict) -> None:
            raise RuntimeError("query orchestration failed")

    monkeypatch.setattr(api, "get_online_flow", FailingFlow)
    client = TestClient(api.app)
    try:
        response = client.post("/query", json={"query": "probe"})
    finally:
        client.close()

    assert response.status_code == 503
    assert response.json()["detail"] == {
        "code": "query_failed",
        "message": "查询处理失败，请稍后重试",
    }
    assert "query orchestration failed" not in response.text


def test_agent_knowledge_tool_calls_knowledge_system_directly(
    monkeypatch: pytest.MonkeyPatch, isolated_runtime: Path
) -> None:
    from src.agent.tools import ToolRegistry, _create_search_kb_tool
    from src.core import knowledge
    from src.core.knowledge import RetrievalResult

    calls: list[dict] = []

    class Knowledge:
        async def retrieve(self, query: str, **kwargs) -> RetrievalResult:
            calls.append({"query": query, **kwargs})
            return RetrievalResult(
                query=query,
                query_variants=[query],
                candidates=[],
                chunks=[
                    {
                        "chunk_id": "chunk-1",
                        "text": "retrieved context",
                        "metadata": {"source": "guide.md"},
                    }
                ],
                index_version="1" * 24,
            )

    monkeypatch.setattr(knowledge, "knowledge_system", Knowledge())
    registry = ToolRegistry(dedup_window=0)
    registry.register(_create_search_kb_tool())
    result = registry.execute(
        "search_knowledge_base",
        {
            "query": "probe",
            "top_k": 1,
            "filter": {"category": "public"},
            "retrieval_mode": "bm25_only",
        },
        "session",
    )

    assert result.success
    assert result.data["context"] == "[1] guide.md\nretrieved context"
    assert result.data["index_version"] == "1" * 24
    assert calls == [
        {
            "query": "probe",
            "top_k": 1,
            "metadata_filter": {"category": "public"},
            "mode": "bm25_only",
        }
    ]


def test_async_knowledge_tool_uses_the_same_interface(
    monkeypatch: pytest.MonkeyPatch, isolated_runtime: Path
) -> None:
    from src.agent.tools import ToolRegistry, _create_search_kb_tool
    from src.core import knowledge
    from src.core.knowledge import RetrievalResult

    class Knowledge:
        async def retrieve(self, query: str, **kwargs) -> RetrievalResult:
            return RetrievalResult(query, [query], [], [])

    monkeypatch.setattr(knowledge, "knowledge_system", Knowledge())
    registry = ToolRegistry(dedup_window=0)
    registry.register(_create_search_kb_tool())

    result = asyncio.run(
        registry.execute_async(
            "search_knowledge_base", {"query": "probe"}, "session"
        )
    )

    assert result.success
    assert result.data["query"] == "probe"


def test_agent_knowledge_tool_hides_retrieval_failure(
    monkeypatch: pytest.MonkeyPatch, isolated_runtime: Path
) -> None:
    from src.agent.tools import ToolRegistry, _create_search_kb_tool
    from src.core import knowledge

    class Knowledge:
        async def retrieve(self, query: str, **kwargs):
            raise RuntimeError("private retrieval failure")

    monkeypatch.setattr(knowledge, "knowledge_system", Knowledge())
    registry = ToolRegistry(dedup_window=0)
    registry.register(_create_search_kb_tool())

    result = asyncio.run(
        registry.execute_async(
            "search_knowledge_base", {"query": "probe"}, "session"
        )
    )

    assert not result.success
    assert result.error_code == "tool_execution_failed"
    assert "private" not in result.error


def test_ablation_flows_use_async_orchestration(
    isolated_runtime: Path,
) -> None:
    from pocketflow import AsyncFlow

    from scripts.run_eval import build_ablation_flows

    assert all(
        isinstance(flow, AsyncFlow)
        for flow, mode in build_ablation_flows().values()
    )


def test_evaluation_propagates_flow_failure(
    monkeypatch: pytest.MonkeyPatch,
    isolated_runtime: Path,
) -> None:
    from scripts import run_eval
    from src.llm.cache import llm_cache

    class FailingFlow:
        async def run_async(self, shared: dict) -> None:
            raise RuntimeError("evaluation flow failed")

    testset = isolated_runtime / "testset.json"
    testset.write_text(
        json.dumps([{"question": "probe", "ground_truth": "expected"}]),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        run_eval,
        "build_ablation_flows",
        lambda: {"A": (FailingFlow(), "vector_only")},
    )
    monkeypatch.setattr(run_eval, "_warmup_bm25", lambda: None)
    monkeypatch.setattr(llm_cache, "clear", lambda: None)
    monkeypatch.setattr(run_eval, "compute_retrieval_metrics", lambda results: {})
    monkeypatch.setattr(run_eval, "run_ragas_eval", lambda results: {})
    monkeypatch.setattr(run_eval, "_write_results", lambda *args: None)

    with pytest.raises(RuntimeError, match="evaluation flow failed"):
        asyncio.run(run_eval.run_ablation(str(testset)))
