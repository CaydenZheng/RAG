"""Embedding and safe publication of versioned vector/BM25 indexes."""

import threading
from typing import Any, Callable, List

from loguru import logger
from pocketflow import BatchNode, Node

from config.settings import settings
from src.core.errors import IndexBuildError
from src.core.index_versions import IndexVersion
from src.core.ingestion import CHUNKER_VERSION, PARSER_VERSION, ChunkerNode
from src.infra.index_catalog import IndexCatalog, index_catalog
from src.llm import llm_client
from src.utils.bm25_store import BM25Store, bm25_store

_INDEX_BUILD_LOCK = threading.Lock()


class EmbedderNode(BatchNode):
    """Attach one embedding to every parsed chunk."""

    def prep(self, shared: dict) -> List[dict]:
        return shared.get("chunks", [])

    def exec(self, chunk: dict) -> dict:
        vector = llm_client.embed_single(chunk["text"])
        return {**chunk, "embedding": vector}

    def post(self, shared: dict, prep_res, exec_res_list: List[dict]) -> str:
        shared["chunks_with_embedding"] = exec_res_list
        logger.info(
            "Embedded {} chunks, dim={}",
            len(exec_res_list),
            llm_client.embedding_dim,
        )
        return "default"


class IndexBuilderNode(Node):
    """Build an invisible candidate and publish it only after validation."""

    BATCH_SIZE = 100

    def __init__(
        self,
        *,
        catalog: IndexCatalog | None = None,
        runtime_bm25: BM25Store | None = None,
        client_factory: Callable[[], Any] | None = None,
        cache: Any | None = None,
    ) -> None:
        super().__init__()
        self._catalog = catalog or index_catalog
        self._runtime_bm25 = runtime_bm25 or bm25_store
        self._client_factory = client_factory or self._create_client
        self._cache = cache

    def prep(self, shared: dict) -> List[dict]:
        return shared.get("chunks_with_embedding", [])

    def exec(self, chunks: List[dict]) -> dict:
        """Serialize builds so candidates cannot overwrite each other."""
        with _INDEX_BUILD_LOCK:
            return self._build_and_publish(chunks)

    def _build_and_publish(self, chunks: List[dict]) -> dict:
        """Build both indexes and atomically switch the active version."""
        try:
            manifest = IndexVersion.create(
                chunks,
                parser=PARSER_VERSION,
                chunker=CHUNKER_VERSION,
                chunk_size=ChunkerNode.CHUNK_SIZE,
                chunk_overlap=ChunkerNode.CHUNK_OVERLAP,
                embedding_model=settings.local_embedding_model,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise IndexBuildError(str(exc)) from exc

        versioned_chunks = self._attach_provenance(chunks, manifest)
        ids = [chunk["chunk_id"] for chunk in versioned_chunks]
        texts = [chunk["text"] for chunk in versioned_chunks]
        candidate_bm25 = BM25Store()
        candidate_bm25.build(
            texts,
            ids,
            version_id=manifest.version_id,
        )

        client = self._client_factory()
        active_before = self._catalog.capture()
        candidate_created = False
        try:
            if active_before.version_id == manifest.version_id:
                collection = client.get_collection(manifest.collection_name)
                self._validate_collection(collection, ids)
                self._runtime_bm25.activate(candidate_bm25)
                self._update_cache_version(manifest.version_id)
                return self._result(manifest, published=False)

            self._delete_collection_if_present(client, manifest.collection_name)
            collection = client.create_collection(
                name=manifest.collection_name,
                metadata={
                    "hnsw:space": "cosine",
                    "index_version": manifest.version_id,
                    "content_checksum": manifest.content_checksum,
                },
            )
            candidate_created = True
            self._write_collection(collection, versioned_chunks)
            self._validate_collection(collection, ids)

            # Switching the cache namespace early can only cause misses while
            # readers still use the old index; it cannot return stale answers.
            self._update_cache_version(manifest.version_id)
            self._catalog.publish(
                manifest,
                lambda: self._runtime_bm25.activate(candidate_bm25),
            )
            logger.info(
                "Published index version {} with {} chunks",
                manifest.version_id,
                manifest.chunk_count,
            )
            return self._result(manifest, published=True)
        except Exception as exc:
            if candidate_created:
                self._delete_collection_if_present(
                    client,
                    manifest.collection_name,
                )
            if isinstance(exc, IndexBuildError):
                raise
            raise IndexBuildError(
                f"index candidate {manifest.version_id} failed validation"
            ) from exc

    def _write_collection(self, collection: Any, chunks: List[dict]) -> None:
        for start in range(0, len(chunks), self.BATCH_SIZE):
            batch = chunks[start : start + self.BATCH_SIZE]
            collection.add(
                ids=[chunk["chunk_id"] for chunk in batch],
                embeddings=[chunk["embedding"] for chunk in batch],
                documents=[chunk["text"] for chunk in batch],
                metadatas=[chunk["metadata"] for chunk in batch],
            )

    @staticmethod
    def _validate_collection(collection: Any, expected_ids: List[str]) -> None:
        if collection.count() != len(expected_ids):
            raise IndexBuildError("candidate chunk count does not match input")
        stored = collection.get(include=[])
        if set(stored.get("ids", [])) != set(expected_ids):
            raise IndexBuildError("candidate chunk IDs do not match input")

    @staticmethod
    def _attach_provenance(
        chunks: List[dict],
        manifest: IndexVersion,
    ) -> List[dict]:
        versioned: List[dict] = []
        for chunk in chunks:
            document_id = str(chunk["doc_id"])
            metadata = {
                **dict(chunk.get("metadata") or {}),
                "source_version": manifest.source_checksum(document_id),
                "index_version": manifest.version_id,
            }
            versioned.append({**chunk, "metadata": metadata})
        return versioned

    def _update_cache_version(self, version_id: str) -> None:
        if self._cache is None:
            from src.llm.cache import llm_cache

            cache = llm_cache
        else:
            cache = self._cache
        cache.update_fingerprint(version_id)

    @staticmethod
    def _delete_collection_if_present(client: Any, name: str) -> None:
        try:
            client.delete_collection(name)
        except Exception:
            return

    @staticmethod
    def _result(manifest: IndexVersion, *, published: bool) -> dict:
        return {
            "chunks_count": manifest.chunk_count,
            "fingerprint": manifest.version_id,
            "version_id": manifest.version_id,
            "content_checksum": manifest.content_checksum,
            "collection_name": manifest.collection_name,
            "published": published,
        }

    @staticmethod
    def _create_client() -> Any:
        import chromadb
        from chromadb.config import Settings as ChromaSettings

        return chromadb.PersistentClient(
            path=str(settings.chroma_path.resolve()),
            settings=ChromaSettings(anonymized_telemetry=False),
        )

    def post(self, shared: dict, prep_res, exec_res: dict) -> str:
        shared["index_info"] = exec_res
        shared["knowledge_fingerprint"] = exec_res["fingerprint"]
        logger.info(
            "Index ready: {} chunks, version={}",
            exec_res["chunks_count"],
            exec_res["version_id"],
        )
        return "default"
