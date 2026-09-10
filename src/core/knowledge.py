"""Unified retrieval interface for HTTP, Agent, and evaluation callers."""

from dataclasses import dataclass
from typing import Literal, Protocol

from config.settings import settings
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


class QueryRewriter(Protocol):
    async def rewrite(self, query: str) -> list[str]: ...


class CandidateRetriever(Protocol):
    def search(
        self,
        queries: list[str],
        metadata_filter: dict | None,
        mode: RetrievalMode,
    ) -> list[dict]: ...


class CandidateReranker(Protocol):
    def rerank(self, query: str, candidates: list[dict]) -> list[dict]: ...


@dataclass(frozen=True)
class RetrievalResult:
    """Observable result of one complete retrieval request."""

    query: str
    query_variants: list[str]
    candidates: list[dict]
    chunks: list[dict]


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
        metadata_filter: dict | None = None,
        mode: RetrievalMode = "hybrid+rerank",
    ) -> RetrievalResult:
        """Run the configured retrieval strategy behind one interface."""
        if mode in ("hybrid", "hybrid+rerank"):
            variants = await self._rewriter.rewrite(query)
        else:
            variants = [query]

        candidates = self._retriever.search(
            variants,
            metadata_filter,
            mode,
        )
        if mode == "hybrid+rerank":
            chunks = self._reranker.rerank(query, candidates)
        else:
            chunks = sorted(
                (
                    {
                        **candidate,
                        "rerank_score": candidate.get("rrf_score", 0),
                    }
                    for candidate in candidates
                ),
                key=lambda candidate: candidate["rerank_score"],
                reverse=True,
            )[: settings.rerank_top_k]

        return RetrievalResult(
            query=query,
            query_variants=variants,
            candidates=candidates,
            chunks=chunks,
        )


knowledge_system = KnowledgeSystem()
