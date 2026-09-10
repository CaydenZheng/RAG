"""Contract tests for the unified KnowledgeSystem retrieval seam."""

import asyncio
from pathlib import Path

import pytest


class _Rewriter:
    def __init__(self, events: list) -> None:
        self.events = events

    async def rewrite(self, query: str) -> list[str]:
        self.events.append(("rewrite", query))
        return [query, f"{query}-variant"]


class _Retriever:
    def __init__(self, events: list) -> None:
        self.events = events

    def search(
        self,
        queries: list[str],
        metadata_filter: dict | None,
        mode: str,
        top_k: int,
    ) -> list[dict]:
        self.events.append(
            ("search", queries, metadata_filter, mode, top_k)
        )
        return [
            {
                "chunk_id": "low",
                "text": "lower result",
                "metadata": {"source": "low.md"},
                "rrf_score": 0.2,
            },
            {
                "chunk_id": "high",
                "text": "higher result",
                "metadata": {"source": "high.md"},
                "rrf_score": 0.8,
            },
        ]


class _Reranker:
    def __init__(self, events: list) -> None:
        self.events = events

    def rerank(
        self,
        query: str,
        candidates: list[dict],
        top_k: int,
    ) -> list[dict]:
        self.events.append(
            (
                "rerank",
                query,
                [candidate["chunk_id"] for candidate in candidates],
                top_k,
            )
        )
        return [{**candidates[0], "rerank_score": 0.9}]


def test_full_retrieval_pipeline_has_one_ordered_interface(
    isolated_runtime: Path,
) -> None:
    from src.core.knowledge import KnowledgeSystem

    events: list = []
    system = KnowledgeSystem(
        rewriter=_Rewriter(events),
        retriever=_Retriever(events),
        reranker=_Reranker(events),
    )

    result = asyncio.run(
        system.retrieve(
            "question",
            top_k=1,
            metadata_filter={"category": "public"},
            mode="hybrid+rerank",
        )
    )

    assert result.query_variants == ["question", "question-variant"]
    assert [candidate["chunk_id"] for candidate in result.candidates] == [
        "low",
        "high",
    ]
    assert [chunk["chunk_id"] for chunk in result.chunks] == ["low"]
    assert events == [
        ("rewrite", "question"),
        (
            "search",
            ["question", "question-variant"],
            {"category": "public"},
            "hybrid+rerank",
            1,
        ),
        ("rerank", "question", ["low", "high"], 1),
    ]


@pytest.mark.parametrize(
    ("mode", "rewrites"),
    [("vector_only", False), ("bm25_only", False), ("hybrid", True)],
)
def test_non_reranked_modes_share_fusion_output(
    isolated_runtime: Path, mode: str, rewrites: bool
) -> None:
    from src.core.knowledge import KnowledgeSystem

    events: list = []
    system = KnowledgeSystem(
        rewriter=_Rewriter(events),
        retriever=_Retriever(events),
        reranker=_Reranker(events),
    )

    result = asyncio.run(system.retrieve("question", mode=mode, top_k=1))

    assert [chunk["chunk_id"] for chunk in result.chunks] == ["high"]
    assert all("rerank_score" in chunk for chunk in result.chunks)
    assert any(event[0] == "rewrite" for event in events) is rewrites
    assert not any(event[0] == "rerank" for event in events)


def test_pocketflow_adapter_exposes_result_without_rebuilding_pipeline(
    isolated_runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tiktoken

    monkeypatch.setattr(
        tiktoken,
        "get_encoding",
        lambda name: type("Encoding", (), {"encode": lambda self, text: list(text)})(),
    )

    from src.core.knowledge import RetrievalResult
    from src.orchestration.rag import KnowledgeRetrievalNode

    calls: list[dict] = []

    class FakeKnowledgeSystem:
        async def retrieve(self, query: str, **kwargs) -> RetrievalResult:
            calls.append({"query": query, **kwargs})
            return RetrievalResult(
                query=query,
                query_variants=[query, "variant"],
                candidates=[{"chunk_id": "candidate"}],
                chunks=[{"chunk_id": "kept"}],
            )

    shared = {
        "query": "question",
        "top_k": 2,
        "filter": {"category": "public"},
        "retrieval_mode": "hybrid",
    }

    async def run_adapter() -> None:
        node = KnowledgeRetrievalNode(FakeKnowledgeSystem())
        inputs = await node.prep_async(shared)
        result = await node.exec_async(inputs)
        await node.post_async(shared, inputs, result)

    asyncio.run(run_adapter())

    assert calls == [
        {
            "query": "question",
            "top_k": 2,
            "metadata_filter": {"category": "public"},
            "mode": "hybrid",
        }
    ]
    assert shared["queries"] == ["question", "variant"]
    assert shared["candidates"] == [{"chunk_id": "candidate"}]
    assert shared["retrieved_chunks"] == [{"chunk_id": "kept"}]
