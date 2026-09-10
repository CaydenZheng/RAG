"""Regression tests for index identity and candidate publication."""

from pathlib import Path

import pytest


def _chunks(text: str) -> list[dict]:
    return [
        {
            "chunk_id": "doc-1_chunk0",
            "doc_id": "doc-1",
            "text": text,
            "chunk_index": 0,
            "metadata": {
                "source": "guide.md",
                "category": "public",
                "chunk_index": 0,
            },
            "embedding": [1.0, 0.0],
        }
    ]


class FakeCollection:
    def __init__(self, client, name: str, metadata: dict | None = None) -> None:
        self.client = client
        self.name = name
        self.metadata = metadata or {}
        self.records: dict[str, dict] = {}

    def add(self, *, ids, embeddings, documents, metadatas) -> None:
        if self.client.on_add is not None:
            self.client.on_add(self.name)
        for chunk_id, embedding, document, metadata in zip(
            ids, embeddings, documents, metadatas, strict=True
        ):
            self.records[chunk_id] = {
                "embedding": embedding,
                "document": document,
                "metadata": metadata,
            }
            if self.name in self.client.fail_on_add:
                raise RuntimeError("candidate write failed")

    def count(self) -> int:
        return len(self.records)

    def get(self, *, include=None, ids=None, where=None) -> dict:
        selected = ids or list(self.records)
        selected = [
            chunk_id
            for chunk_id in selected
            if chunk_id in self.records
            and (
                where is None
                or all(
                    self.records[chunk_id]["metadata"].get(key) == value
                    for key, value in where.items()
                )
            )
        ]
        return {
            "ids": selected,
            "documents": [self.records[item]["document"] for item in selected],
            "metadatas": [self.records[item]["metadata"] for item in selected],
        }

    def query(self, **kwargs) -> dict:
        selected = list(self.records)[: kwargs["n_results"]]
        return {
            "ids": [selected],
            "documents": [[self.records[item]["document"] for item in selected]],
            "distances": [[0.1 for _ in selected]],
            "metadatas": [[self.records[item]["metadata"] for item in selected]],
        }


class FakeClient:
    def __init__(self) -> None:
        self.collections: dict[str, FakeCollection] = {}
        self.fail_on_add: set[str] = set()
        self.on_add = None
        self.requested: list[str] = []

    def create_collection(self, name: str, metadata=None) -> FakeCollection:
        if name in self.collections:
            raise ValueError("collection already exists")
        collection = FakeCollection(self, name, metadata)
        self.collections[name] = collection
        return collection

    def get_collection(self, name: str) -> FakeCollection:
        self.requested.append(name)
        return self.collections[name]

    def delete_collection(self, name: str) -> None:
        if name not in self.collections:
            raise KeyError(name)
        del self.collections[name]


class FakeCache:
    def __init__(self) -> None:
        self.versions: list[str] = []

    def update_fingerprint(self, version_id: str) -> None:
        self.versions.append(version_id)


def test_failed_candidate_never_replaces_active_index(
    isolated_runtime: Path,
) -> None:
    from config.settings import settings
    from src.core.errors import IndexBuildError
    from src.core.index_versions import (
        LEGACY_INDEX_VERSION,
        IndexVersion,
    )
    from src.core.indexing import IndexBuilderNode
    from src.core.ingestion import CHUNKER_VERSION, PARSER_VERSION, ChunkerNode
    from src.infra.index_catalog import IndexCatalog
    from src.utils.bm25_store import BM25Store

    catalog = IndexCatalog(isolated_runtime / "manifests")
    runtime_bm25 = BM25Store()
    client = FakeClient()
    cache = FakeCache()
    observed_active: list[str] = []
    client.on_add = lambda name: observed_active.append(
        catalog.capture().version_id
    )
    builder = IndexBuilderNode(
        catalog=catalog,
        runtime_bm25=runtime_bm25,
        client_factory=lambda: client,
        cache=cache,
    )

    first_info = builder.exec(_chunks("published content"))
    first_active = catalog.capture()
    first_collection = client.collections[first_active.collection_name]

    next_manifest = IndexVersion.create(
        _chunks("broken replacement"),
        parser=PARSER_VERSION,
        chunker=CHUNKER_VERSION,
        chunk_size=ChunkerNode.CHUNK_SIZE,
        chunk_overlap=ChunkerNode.CHUNK_OVERLAP,
        embedding_model=settings.local_embedding_model,
    )
    client.fail_on_add.add(next_manifest.collection_name)

    with pytest.raises(IndexBuildError):
        builder.exec(_chunks("broken replacement"))

    assert observed_active == [LEGACY_INDEX_VERSION, first_active.version_id]
    assert catalog.capture() == first_active
    assert first_info["published"] is True
    assert first_collection.metadata["index_version"] == first_active.version_id
    assert next_manifest.collection_name not in client.collections
    assert runtime_bm25.version_id == first_active.version_id
    assert cache.versions == [first_active.version_id]
    stored_metadata = next(iter(first_collection.records.values()))["metadata"]
    assert stored_metadata["index_version"] == first_active.version_id
    assert stored_metadata["source_version"] == (
        first_active.manifest.sources[0].checksum
    )


def test_failed_pointer_commit_keeps_old_runtime_index(
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from config.settings import settings
    from src.core.errors import IndexBuildError
    from src.core.index_versions import IndexVersion
    from src.core.indexing import IndexBuilderNode
    from src.core.ingestion import CHUNKER_VERSION, PARSER_VERSION, ChunkerNode
    from src.infra.index_catalog import IndexCatalog
    from src.utils.bm25_store import BM25Store

    catalog = IndexCatalog(isolated_runtime / "manifests")
    runtime_bm25 = BM25Store()
    client = FakeClient()
    cache = FakeCache()
    builder = IndexBuilderNode(
        catalog=catalog,
        runtime_bm25=runtime_bm25,
        client_factory=lambda: client,
        cache=cache,
    )
    builder.exec(_chunks("published content"))
    active_before = catalog.capture()

    next_manifest = IndexVersion.create(
        _chunks("replacement content"),
        parser=PARSER_VERSION,
        chunker=CHUNKER_VERSION,
        chunk_size=ChunkerNode.CHUNK_SIZE,
        chunk_overlap=ChunkerNode.CHUNK_OVERLAP,
        embedding_model=settings.local_embedding_model,
    )
    write_json_atomic = catalog._write_json_atomic

    def fail_active_pointer(path: Path, payload: dict) -> None:
        if path.name == "active.json":
            raise OSError("pointer commit failed")
        write_json_atomic(path, payload)

    monkeypatch.setattr(catalog, "_write_json_atomic", fail_active_pointer)

    with pytest.raises(IndexBuildError):
        builder.exec(_chunks("replacement content"))

    assert catalog.capture() == active_before
    assert runtime_bm25.version_id == active_before.version_id
    assert cache.versions == [active_before.version_id]
    assert next_manifest.collection_name not in client.collections


def test_rollback_activates_previous_validated_version(
    isolated_runtime: Path,
) -> None:
    from src.core.indexing import IndexBuilderNode
    from src.infra.index_catalog import IndexCatalog
    from src.utils.bm25_store import BM25Store

    catalog = IndexCatalog(isolated_runtime / "manifests")
    runtime_bm25 = BM25Store()
    client = FakeClient()
    cache = FakeCache()
    builder = IndexBuilderNode(
        catalog=catalog,
        runtime_bm25=runtime_bm25,
        client_factory=lambda: client,
        cache=cache,
    )

    first = builder.exec(_chunks("first version"))
    second = builder.exec(_chunks("second version"))

    assert catalog.previous().version_id == first["version_id"]
    result = builder.rollback()

    assert result["version_id"] == first["version_id"]
    assert result["rolled_back_from"] == second["version_id"]
    assert catalog.capture().version_id == first["version_id"]
    assert catalog.previous().version_id == second["version_id"]
    assert runtime_bm25.version_id == first["version_id"]
    assert cache.versions == [
        first["version_id"],
        second["version_id"],
        first["version_id"],
    ]


def test_rollback_rejects_a_missing_retained_collection(
    isolated_runtime: Path,
) -> None:
    from src.core.errors import IndexBuildError
    from src.core.indexing import IndexBuilderNode
    from src.infra.index_catalog import IndexCatalog
    from src.utils.bm25_store import BM25Store

    catalog = IndexCatalog(isolated_runtime / "manifests")
    runtime_bm25 = BM25Store()
    client = FakeClient()
    builder = IndexBuilderNode(
        catalog=catalog,
        runtime_bm25=runtime_bm25,
        client_factory=lambda: client,
        cache=FakeCache(),
    )
    first = builder.exec(_chunks("first version"))
    second = builder.exec(_chunks("second version"))
    client.delete_collection(first["collection_name"])

    with pytest.raises(IndexBuildError):
        builder.rollback()

    assert catalog.capture().version_id == second["version_id"]
    assert runtime_bm25.version_id == second["version_id"]


def test_empty_document_set_publishes_an_empty_index(
    isolated_runtime: Path,
    fixed_embedder,
) -> None:
    from src.core.indexing import IndexBuilderNode
    from src.infra.index_catalog import IndexCatalog
    from src.utils.bm25_store import BM25Store

    fixed_embedder.vectors["dimension-probe"] = [1.0, 0.0]
    catalog = IndexCatalog(isolated_runtime / "manifests")
    runtime_bm25 = BM25Store()
    client = FakeClient()
    result = IndexBuilderNode(
        catalog=catalog,
        runtime_bm25=runtime_bm25,
        client_factory=lambda: client,
        cache=FakeCache(),
    ).exec([])

    active = catalog.capture()
    assert result["chunks_count"] == 0
    assert active.manifest.sources == ()
    assert client.collections[active.collection_name].count() == 0
    assert runtime_bm25.version_id == active.version_id
    assert runtime_bm25.is_ready is False


def test_offline_flow_publishes_empty_index_after_last_document_deleted(
    isolated_runtime: Path,
    fixed_embedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    import tiktoken

    monkeypatch.setattr(
        tiktoken,
        "get_encoding",
        lambda name: SimpleNamespace(encode=lambda text: []),
    )

    from src.orchestration.rag import create_offline_flow

    fixed_embedder.vectors["dimension-probe"] = [1.0, 0.0]
    shared: dict = {}
    create_offline_flow().run(shared)

    assert shared["index_info"]["chunks_count"] == 0
    assert shared["index_info"]["version_id"]


def test_published_manifest_matches_real_chroma_collection(
    isolated_runtime: Path,
) -> None:
    import chromadb

    from config.settings import settings
    from src.core.indexing import IndexBuilderNode
    from src.infra.index_catalog import IndexCatalog
    from src.utils.bm25_store import BM25Store

    catalog = IndexCatalog(isolated_runtime / "manifests")
    builder = IndexBuilderNode(
        catalog=catalog,
        runtime_bm25=BM25Store(),
        cache=FakeCache(),
    )

    info = builder.exec(_chunks("real chroma candidate"))
    client = chromadb.PersistentClient(
        path=str(settings.chroma_path.resolve()),
        settings=chromadb.config.Settings(anonymized_telemetry=False),
    )
    collection = client.get_collection(info["collection_name"])
    stored = collection.get()

    assert collection.count() == 1
    assert stored["ids"] == ["doc-1_chunk0"]
    assert stored["metadatas"][0]["index_version"] == info["version_id"]
    assert catalog.capture().collection_name == info["collection_name"]


def test_retrieval_captures_one_collection_for_the_whole_request(
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.core import retrieval
    from src.core.index_versions import ActiveIndex

    client = FakeClient()
    collection = client.create_collection("rag_v_first")
    collection.records["allowed"] = {
        "embedding": [1.0, 0.0],
        "document": "public content",
        "metadata": {"category": "public"},
    }

    class ChangingCatalog:
        def __init__(self) -> None:
            self.calls = 0

        def capture(self) -> ActiveIndex:
            self.calls += 1
            return ActiveIndex("1" * 24, "rag_v_first")

    class VersionedBM25:
        def search(self, *args, **kwargs):
            assert kwargs["version_id"] == "1" * 24
            return []

    catalog = ChangingCatalog()
    monkeypatch.setattr(retrieval, "index_catalog", catalog)
    monkeypatch.setattr(
        retrieval.chromadb,
        "PersistentClient",
        lambda **kwargs: client,
    )
    monkeypatch.setattr(retrieval, "bm25_store", VersionedBM25())
    monkeypatch.setattr(retrieval.llm_client, "embed_single", lambda query: [1.0, 0.0])

    results = retrieval.HybridRetrieverNode().search(
        ["question", "variant"],
        {"category": "public"},
        "hybrid",
        top_k=1,
    )

    assert catalog.calls == 1
    assert results.index_version == "1" * 24
    assert client.requested == ["rag_v_first"]
    assert [item["chunk_id"] for item in results] == ["allowed"]


def test_bm25_rejects_a_different_index_version(
    isolated_runtime: Path,
) -> None:
    from src.utils.bm25_store import BM25Store

    store = BM25Store()
    store.build(
        ["versioned keyword", "unrelated alpha", "unrelated beta"],
        ["chunk-1", "chunk-2", "chunk-3"],
        version_id="v1",
    )

    assert store.search("keyword", version_id="v2") == []
    matching = store.search("keyword", version_id="v1")
    assert matching and matching[0][0] == "chunk-1"
