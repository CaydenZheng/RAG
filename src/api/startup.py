"""Application startup warm-up and readiness state."""

from __future__ import annotations

import threading
from typing import Any, Literal, TypedDict

import chromadb
from loguru import logger

from config.settings import settings
from src.core.index_versions import ActiveIndex
from src.infra.index_catalog import index_catalog

ComponentStatus = Literal[
    "pending", "loading", "ready", "degraded", "skipped", "failed"
]
RuntimeStatus = Literal["starting", "ready", "degraded", "unavailable"]


class ComponentReadiness(TypedDict):
    """Public state for one startup dependency."""

    status: ComponentStatus
    code: str | None
    message: str


class ReadinessSnapshot(TypedDict):
    """Stable readiness payload returned by the HTTP endpoint."""

    status: RuntimeStatus
    components: dict[str, ComponentReadiness]


COMPONENT_NAMES: tuple[str, ...] = ("embedding", "index", "bm25", "reranker")
REQUIRED_COMPONENTS: frozenset[str] = frozenset({"embedding", "index"})


class RuntimeReadiness:
    """Thread-safe readiness state shared by startup tasks and HTTP handlers."""

    def __init__(self) -> None:
        self._lock: threading.RLock = threading.RLock()
        self._components: dict[str, ComponentReadiness] = {}
        self.reset()

    def reset(self) -> None:
        """Reset every component before a new application startup."""
        with self._lock:
            self._components = {
                name: {
                    "status": "pending",
                    "code": None,
                    "message": "warm-up has not started",
                }
                for name in COMPONENT_NAMES
            }

    def update(
        self,
        component: str,
        status: ComponentStatus,
        *,
        code: str | None = None,
        message: str,
    ) -> None:
        """Publish one component transition without exposing exception details."""
        if component not in COMPONENT_NAMES:
            raise ValueError(f"unknown readiness component: {component}")
        with self._lock:
            self._components[component] = {
                "status": status,
                "code": code,
                "message": message,
            }

    def snapshot(self) -> ReadinessSnapshot:
        """Return an isolated snapshot and derive overall service availability."""
        with self._lock:
            components: dict[str, ComponentReadiness] = {
                name: state.copy() for name, state in self._components.items()
            }

        required_states: set[ComponentStatus] = {
            components[name]["status"] for name in REQUIRED_COMPONENTS
        }
        status: RuntimeStatus
        if "failed" in required_states:
            status = "unavailable"
        elif required_states != {"ready"}:
            status = "starting"
        elif any(
            state["status"] != "ready"
            for name, state in components.items()
            if name not in REQUIRED_COMPONENTS
        ):
            status = "degraded"
        else:
            status = "ready"
        return {"status": status, "components": components}


runtime_readiness = RuntimeReadiness()


def _failure_details(component: str, error: Exception) -> tuple[str, str]:
    if isinstance(error, MemoryError):
        return (
            "insufficient_memory",
            f"{component} could not be loaded within available memory; "
            "free memory or use a smaller model",
        )
    return (
        f"{component}_unavailable",
        f"{component} warm-up failed; check model files and configuration",
    )


def _mark_failure(component: str, error: Exception, *, required: bool) -> None:
    code: str
    message: str
    code, message = _failure_details(component, error)
    status: ComponentStatus = "failed" if required else "degraded"
    runtime_readiness.update(component, status, code=code, message=message)
    logger.warning("{} warm-up failed: {}", component, type(error).__name__)


def _preload_embedding() -> bool:
    runtime_readiness.update(
        "embedding",
        "loading",
        message=f"loading embedding model {settings.local_embedding_model}",
    )
    try:
        from src.llm import llm_client

        dimension: int = llm_client.embedding_dim
    except Exception as error:
        _mark_failure("embedding", error, required=True)
        return False

    runtime_readiness.update(
        "embedding",
        "ready",
        message=f"embedding model ready ({dimension} dimensions)",
    )
    logger.info("Embedding model ready: {} dimensions", dimension)
    return True


def _preload_reranker() -> bool:
    runtime_readiness.update(
        "reranker",
        "loading",
        message=f"loading reranker model {settings.rerank_model}",
    )
    try:
        from src.core.retrieval import _get_reranker

        _get_reranker()
    except Exception as error:
        _mark_failure("reranker", error, required=False)
        return False

    runtime_readiness.update(
        "reranker",
        "ready",
        message="reranker model ready",
    )
    logger.info("Reranker model ready")
    return True


def _prepare_index() -> bool:
    runtime_readiness.update(
        "index",
        "loading",
        message="opening the active vector index",
    )
    runtime_readiness.update(
        "bm25",
        "pending",
        message="waiting for the active index",
    )
    try:
        persist_dir: str = str(settings.chroma_path.resolve())
        client: Any = chromadb.PersistentClient(
            path=persist_dir,
            settings=chromadb.config.Settings(anonymized_telemetry=False),
        )
        active_index: ActiveIndex = index_catalog.capture()
        collection: Any = client.get_collection(active_index.collection_name)
        document_count: int = collection.count()
    except Exception as error:
        _mark_failure("index", error, required=True)
        runtime_readiness.update(
            "bm25",
            "skipped",
            code="index_unavailable",
            message="BM25 rebuild skipped because the active index is unavailable",
        )
        return False

    runtime_readiness.update(
        "index",
        "ready",
        message=f"active index ready ({document_count} chunks)",
    )
    if document_count == 0:
        runtime_readiness.update(
            "bm25",
            "skipped",
            code="empty_index",
            message="BM25 rebuild skipped because the active index is empty",
        )
        logger.info("ChromaDB is empty, skipping BM25 rebuild")
        return True

    runtime_readiness.update(
        "bm25",
        "loading",
        message=f"rebuilding BM25 from {document_count} chunks",
    )
    try:
        from src.utils.bm25_store import bm25_store

        all_data: dict[str, Any] = collection.get()
        texts: list[str] = list(all_data["documents"] or [])
        chunk_ids: list[str] = list(all_data["ids"] or [])
        bm25_store.build(
            texts,
            chunk_ids,
            version_id=active_index.version_id,
        )
    except Exception as error:
        _mark_failure("bm25", error, required=False)
        return True

    runtime_readiness.update(
        "bm25",
        "ready",
        message=f"BM25 ready ({len(texts)} chunks)",
    )
    logger.info(
        "BM25 ready: {} docs, version={}",
        len(texts),
        active_index.version_id,
    )
    return True


def _warm_up_components() -> None:
    """Warm required resources first and avoid concurrent model memory spikes."""
    embedding_ready: bool = _preload_embedding()
    _prepare_index()
    if not settings.startup_preload_reranker:
        runtime_readiness.update(
            "reranker",
            "skipped",
            code="preload_disabled",
            message="reranker preload disabled; avoid reranked modes on low-memory hosts",
        )
    elif not embedding_ready:
        runtime_readiness.update(
            "reranker",
            "skipped",
            code="required_warmup_failed",
            message="optional reranker preload skipped after embedding failure",
        )
    else:
        _preload_reranker()


def warm_up_runtime() -> None:
    """Start one ordered background warm-up without blocking HTTP startup."""
    runtime_readiness.reset()
    warmup_thread: threading.Thread = threading.Thread(
        target=_warm_up_components,
        name="runtime-warmup",
        daemon=True,
    )
    warmup_thread.start()
