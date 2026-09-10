"""Unified retrieval interface for HTTP, Agent, and evaluation callers."""

import asyncio
from dataclasses import dataclass
from typing import Literal, Protocol, cast

from chromadb.api.types import validate_where
from loguru import logger

from config.settings import settings
from src.core.index_versions import (
    LEGACY_INDEX_VERSION,
    CandidateBatch,
)
from src.core.retrieval import (
    HybridRetrieverNode,
    QueryRewriterNode,
    RerankerNode,
)

RetrievalMode = Literal[
    "vector_only",
    "bm25_only",
    "hybrid",
    "hybrid+rerank",
]

RETRIEVAL_MODES = frozenset(
    {"vector_only", "bm25_only", "hybrid", "hybrid+rerank"}
)
DEFAULT_RETRIEVAL_MODE: RetrievalMode = "hybrid+rerank"
DEFAULT_RETRIEVAL_TOP_K = 5
MAX_RETRIEVAL_TOP_K = 20


def validate_retrieval_mode(mode: str) -> RetrievalMode:
    """Return a supported retrieval mode or raise a public-safe error."""
    if not isinstance(mode, str) or mode not in RETRIEVAL_MODES:
        choices = ", ".join(sorted(RETRIEVAL_MODES))
        raise ValueError(f"retrieval_mode must be one of: {choices}")
    return cast(RetrievalMode, mode)


def validate_retrieval_top_k(top_k: int) -> int:
    """Keep the public result budget within the supported candidate window."""
    if isinstance(top_k, bool) or not isinstance(top_k, int):
        raise ValueError("top_k must be an integer")
    if not 1 <= top_k <= MAX_RETRIEVAL_TOP_K:
        raise ValueError(
            f"top_k must be between 1 and {MAX_RETRIEVAL_TOP_K}"
        )
    return top_k


def validate_metadata_filter(
    metadata_filter: dict | None,
) -> dict | None:
    """Validate the Chroma where grammar before retrieval starts."""
    if metadata_filter is None:
        return None
    if not isinstance(metadata_filter, dict):
        raise ValueError("filter must be an object")
    try:
        validate_where(metadata_filter)
    except (TypeError, ValueError) as exc:
        raise ValueError("filter is not a valid metadata filter") from exc
    return metadata_filter


class QueryRewriter(Protocol):
    async def rewrite(self, query: str) -> list[str]: ...


class CandidateRetriever(Protocol):
    def search(
        self,
        queries: list[str],
        metadata_filter: dict | None,
        mode: RetrievalMode,
        top_k: int,
    ) -> CandidateBatch | list[dict]: ...


class CandidateReranker(Protocol):
    def rerank(
        self,
        query: str,
        candidates: list[dict],
        top_k: int,
    ) -> list[dict]: ...


@dataclass(frozen=True)
class RetrievalResult:
    """Observable result of one complete retrieval request."""

    query: str
    query_variants: list[str]
    candidates: list[dict]
    chunks: list[dict]
    warnings: tuple[str, ...] = ()
    index_version: str = LEGACY_INDEX_VERSION


class KnowledgeSystem:
    """Own query rewriting, Dense/BM25 retrieval, RRF, and reranking."""

    def __init__(
        self,
        rewriter: QueryRewriter | None = None,
        retriever: CandidateRetriever | None = None,
        reranker: CandidateReranker | None = None,
    ) -> None:
        self._rewriter = rewriter or QueryRewriterNode()
        self._retriever = retriever or HybridRetrieverNode()
        self._reranker = reranker or RerankerNode()

    async def retrieve(
        self,
        query: str,
        *,
        top_k: int = DEFAULT_RETRIEVAL_TOP_K,
        metadata_filter: dict | None = None,
        mode: RetrievalMode = DEFAULT_RETRIEVAL_MODE,
    ) -> RetrievalResult:
        """Run one validated retrieval strategy behind a single interface."""
        top_k = validate_retrieval_top_k(top_k)
        mode = validate_retrieval_mode(mode)
        metadata_filter = validate_metadata_filter(metadata_filter)

        if mode in ("hybrid", "hybrid+rerank"):
            variants = await self._rewriter.rewrite(query)
        else:
            variants = [query]

        candidate_result = await asyncio.to_thread(
            self._retriever.search,
            variants,
            metadata_filter,
            mode,
            top_k,
        )
        if isinstance(candidate_result, CandidateBatch):
            candidates = list(candidate_result)
            index_version = candidate_result.index_version
        else:
            candidates = candidate_result
            index_version = LEGACY_INDEX_VERSION
        warnings: list[str] = []
        if mode == "hybrid+rerank" and candidates:
            try:
                chunks = await asyncio.wait_for(
                    asyncio.to_thread(
                        self._reranker.rerank,
                        query,
                        candidates,
                        top_k,
                    ),
                    timeout=settings.rerank_timeout_seconds,
                )
            except TimeoutError:
                logger.warning(
                    "Rerank timed out after {}s; using fusion order",
                    settings.rerank_timeout_seconds,
                )
                warnings.append("rerank_timeout")
                chunks = self._rank_without_reranker(candidates, top_k)
            except Exception:
                logger.exception("Rerank failed; using fusion order")
                warnings.append("rerank_unavailable")
                chunks = self._rank_without_reranker(candidates, top_k)
        else:
            chunks = self._rank_without_reranker(candidates, top_k)

        return RetrievalResult(
            query=query,
            query_variants=variants,
            candidates=candidates,
            chunks=chunks,
            warnings=tuple(warnings),
            index_version=index_version,
        )

    @staticmethod
    def _rank_without_reranker(
        candidates: list[dict],
        top_k: int,
    ) -> list[dict]:
        return sorted(
            (
                {
                    **candidate,
                    "rerank_score": candidate.get("rrf_score", 0),
                }
                for candidate in candidates
            ),
            key=lambda candidate: candidate["rerank_score"],
            reverse=True,
        )[:top_k]


knowledge_system = KnowledgeSystem()
