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

    assert response.status_code == 500
    assert response.json()["detail"] == "query orchestration failed"


def test_agent_knowledge_tool_awaits_online_flow(
    monkeypatch: pytest.MonkeyPatch, isolated_runtime: Path
) -> None:
    import flow
    from src.agent.tools import ToolRegistry, _create_search_kb_tool

    calls: list[dict] = []

    class SuccessfulFlow:
        async def run_async(self, shared: dict) -> None:
            calls.append(shared.copy())
            shared.update(
                answer="answer",
                context="context",
                sources=[{"id": 1}, {"id": 2}],
            )

    monkeypatch.setattr(flow, "get_online_flow", SuccessfulFlow)

    registry = ToolRegistry(dedup_window=0)
    registry.register(_create_search_kb_tool())
    result = registry.execute(
        "search_knowledge_base", {"query": "probe", "top_k": 1}, "session"
    )

    assert result.success
    assert result.data == {
        "answer": "answer",
        "context": "context",
        "sources": [{"id": 1}],
    }
    assert calls == [{"query": "probe"}]


def test_agent_knowledge_tool_preserves_flow_failure(
    monkeypatch: pytest.MonkeyPatch, isolated_runtime: Path
) -> None:
    import flow
    from src.agent.tools import ToolRegistry, _create_search_kb_tool

    class FailingFlow:
        async def run_async(self, shared: dict) -> None:
            raise RuntimeError("knowledge retrieval failed")

    monkeypatch.setattr(flow, "get_online_flow", FailingFlow)

    registry = ToolRegistry(dedup_window=0)
    registry.register(_create_search_kb_tool())
    result = registry.execute(
        "search_knowledge_base", {"query": "probe"}, "session"
    )

    assert not result.success
    assert result.error == "knowledge retrieval failed"


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
