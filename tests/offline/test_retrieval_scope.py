"""Regression tests for retrieval metadata scope enforcement."""

from pathlib import Path

import pytest


def test_unbuilt_bm25_is_unavailable_before_version_comparison(
    isolated_runtime: Path,
) -> None:
    from src.utils.bm25_store import BM25Store

    result = BM25Store().search_with_status(
        "question",
        version_id="versioned-index",
    )

    assert result.status == "unavailable"


def test_hybrid_retriever_reports_unavailable_bm25(
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.core import retrieval
    from src.utils.bm25_store import BM25Store

    class EmptyCollection:
        def query(self, **kwargs) -> dict:
            return {
                "ids": [[]],
                "documents": [[]],
                "distances": [[]],
                "metadatas": [[]],
            }

    class FakeClient:
        def get_collection(self, name: str) -> EmptyCollection:
            return EmptyCollection()

    monkeypatch.setattr(
        retrieval.chromadb,
        "PersistentClient",
        lambda **kwargs: FakeClient(),
    )
    monkeypatch.setattr(retrieval.llm_client, "embed_single", lambda query: [0.1])
    monkeypatch.setattr(retrieval, "bm25_store", BM25Store())

    result = retrieval.HybridRetrieverNode().search(
        ["question"],
        None,
        "hybrid",
    )

    assert result.dense_status == "available"
    assert result.bm25_status == "unavailable"



def test_hybrid_retriever_keeps_degradation_across_query_variants(
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.core import retrieval
    from src.utils.bm25_store import BM25SearchResult

    class EmptyCollection:
        pass

    class FakeClient:
        def get_collection(self, name: str) -> EmptyCollection:
            return EmptyCollection()

    class TransitioningBM25:
        def __init__(self) -> None:
            self.results = iter(
                [
                    BM25SearchResult((), "unavailable"),
                    BM25SearchResult((), "available"),
                ]
            )

        def search_with_status(self, *args, **kwargs) -> BM25SearchResult:
            return next(self.results)

    monkeypatch.setattr(
        retrieval.chromadb,
        "PersistentClient",
        lambda **kwargs: FakeClient(),
    )
    monkeypatch.setattr(retrieval, "bm25_store", TransitioningBM25())

    result = retrieval.HybridRetrieverNode().search(
        ["question", "variant"],
        None,
        "bm25_only",
    )

    assert result.bm25_status == "unavailable"


def test_hybrid_retriever_distinguishes_bm25_mismatch_from_zero_hits(
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.core import retrieval
    from src.utils.bm25_store import BM25Store


    class EmptyCollection:
        pass

    class FakeClient:
        def get_collection(self, name: str) -> EmptyCollection:
            return EmptyCollection()

    store = BM25Store()
    texts = ["unrelated alpha", "unrelated beta", "unrelated gamma"]
    chunk_ids = ["chunk-1", "chunk-2", "chunk-3"]
    store.build(texts, chunk_ids, version_id="different-version")
    monkeypatch.setattr(
        retrieval.chromadb,
        "PersistentClient",
        lambda **kwargs: FakeClient(),
    )
    monkeypatch.setattr(retrieval, "bm25_store", store)

    mismatch = retrieval.HybridRetrieverNode().search(
        ["question"],
        None,
        "bm25_only",
    )

    store.build(texts, chunk_ids, version_id="legacy")
    zero_hits = retrieval.HybridRetrieverNode().search(
        ["question"],
        None,
        "bm25_only",
    )

    assert mismatch.bm25_status == "version_mismatch"
    assert zero_hits.bm25_status == "available"
    assert list(zero_hits) == []


def test_bm25_applies_scope_before_top_k(isolated_runtime: Path) -> None:
    from src.utils.bm25_store import BM25Store

    class FixedScores:
        def get_scores(self, tokens: list[str]) -> list[float]:
            return [10.0, 8.0, 6.0]

    store = BM25Store()
    store._bm25 = FixedScores()
    store._chunk_ids = ["blocked-1", "blocked-2", "allowed"]
    store._ready = True

    results = store.search(
        "query",
        top_k=1,
        allowed_chunk_ids={"allowed"},
    )

    assert results == [("allowed", 6.0)]


def test_hybrid_retrieval_keeps_one_scope_across_all_paths(
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.core import retrieval
    from src.utils.bm25_store import BM25SearchResult

    scope = {"category": "public"}
    collection_calls: list[tuple[str, dict]] = []
    bm25_calls: list[dict] = []

    class FakeCollection:
        def get(self, **kwargs) -> dict:
            collection_calls.append(("get", kwargs))
            if "ids" not in kwargs:
                return {"ids": ["allowed-vector", "allowed-bm25"]}
            if kwargs["ids"] == ["allowed-bm25"]:
                return {
                    "ids": ["allowed-bm25"],
                    "documents": ["allowed keyword text"],
                    "metadatas": [{"category": "public"}],
                }
            raise AssertionError(f"unexpected chunk fetch: {kwargs}")

        def query(self, **kwargs) -> dict:
            collection_calls.append(("query", kwargs))
            return {
                "ids": [["allowed-vector", "blocked-vector"]],
                "documents": [["allowed vector text", "blocked vector text"]],
                "distances": [[0.1, 0.2]],
                "metadatas": [
                    [
                        {"category": "public"},
                        {"category": "private"},
                    ]
                ],
            }

    class FakeClient:
        def get_collection(self, name: str) -> FakeCollection:
            assert name == "rag_collection"
            return FakeCollection()

    def fake_bm25_search(
        query: str,
        top_k: int,
        *,
        allowed_chunk_ids: set[str] | None,
        version_id: str,
    ) -> BM25SearchResult:
        bm25_calls.append(
            {
                "query": query,
                "top_k": top_k,
                "allowed_chunk_ids": allowed_chunk_ids,
                "version_id": version_id,
            }
        )
        return BM25SearchResult(
            (("blocked-bm25", 9.0), ("allowed-bm25", 5.0)),
            "available",
        )

    monkeypatch.setattr(
        retrieval.chromadb,
        "PersistentClient",
        lambda **kwargs: FakeClient(),
    )
    monkeypatch.setattr(retrieval.llm_client, "embed_single", lambda query: [0.1])
    monkeypatch.setattr(
        retrieval.bm25_store,
        "search_with_status",
        fake_bm25_search,
    )

    results = retrieval.HybridRetrieverNode().search(
        ["question"],
        scope,
        "hybrid",
    )

    assert {item["chunk_id"] for item in results} == {
        "allowed-vector",
        "allowed-bm25",
    }
    assert bm25_calls == [
        {
            "query": "question",
            "top_k": retrieval.settings.bm25_top_k,
            "allowed_chunk_ids": {"allowed-vector", "allowed-bm25"},
            "version_id": "legacy",
        }
    ]

    scope_reads = [
        kwargs
        for operation, kwargs in collection_calls
        if operation == "get" and "ids" not in kwargs
    ]
    assert scope_reads == [{"where": scope, "include": []}]

    vector_queries = [
        kwargs for operation, kwargs in collection_calls if operation == "query"
    ]
    assert len(vector_queries) == 1
    assert vector_queries[0]["where"] == scope

    chunk_reads = [
        kwargs
        for operation, kwargs in collection_calls
        if operation == "get" and "ids" in kwargs
    ]
    assert chunk_reads == [{"ids": ["allowed-bm25"], "where": scope}]


def test_bm25_only_scope_uses_real_chroma_metadata(
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from config.settings import settings
    from src.core import retrieval
    from src.utils.bm25_store import BM25Store

    client = retrieval.chromadb.PersistentClient(
        path=str(settings.chroma_path.resolve()),
        settings=retrieval.chromadb.config.Settings(
            anonymized_telemetry=False
        ),
    )
    collection = client.create_collection("rag_collection")
    collection.add(
        ids=["blocked-1", "blocked-2", "allowed"],
        embeddings=[[1.0, 0.0], [1.0, 0.0], [1.0, 0.0]],
        documents=[
            "private noise",
            "other private noise",
            "scopeword public",
        ],
        metadatas=[
            {"category": "private"},
            {"category": "private"},
            {"category": "public"},
        ],
    )

    scoped_bm25 = BM25Store()
    scoped_bm25.build(
        ["private noise", "other private noise", "scopeword public"],
        ["blocked-1", "blocked-2", "allowed"],
    )
    monkeypatch.setattr(retrieval, "bm25_store", scoped_bm25)

    results = retrieval.HybridRetrieverNode().search(
        ["scopeword"],
        {"category": "public"},
        "bm25_only",
    )

    assert [item["chunk_id"] for item in results] == ["allowed"]
    assert results[0]["text"] == "scopeword public"
    assert results[0]["metadata"] == {"category": "public"}
