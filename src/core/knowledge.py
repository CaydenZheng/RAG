"""Unified retrieval interface for HTTP, Agent, and evaluation callers."""

import asyncio
import math
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, cast

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
from src.infra.tracer import tracer

RetrievalMode = Literal[
    "vector_only",
    "bm25_only",
    "hybrid",
    "hybrid+rerank",
]
RETRIEVAL_SCORE_FIELDS: dict[RetrievalMode, str] = {
    "vector_only": "dense_score",
    "bm25_only": "bm25_score",
    "hybrid": "rrf_score",
    "hybrid+rerank": "rerank_score",
}

RETRIEVAL_MODES = frozenset(
    {"vector_only", "bm25_only", "hybrid", "hybrid+rerank"}
)
DEFAULT_RETRIEVAL_MODE: RetrievalMode = "hybrid+rerank"
DEFAULT_RETRIEVAL_TOP_K = 5
MAX_RETRIEVAL_TOP_K = 20
_BM25_WARNING_BY_STATUS: dict[str, str] = {
    "unavailable": "bm25_unavailable",
    "version_mismatch": "bm25_version_mismatch",
}


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
class EvidenceDecision:
    """Explain whether retrieved chunks may be used as answer evidence."""

    sufficient: bool
    reason: str
    score_name: str
    observed_score: float | None
    threshold: float | None
    calibration_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "sufficient": self.sufficient,
            "reason": self.reason,
            "score_name": self.score_name,
            "observed_score": self.observed_score,
            "threshold": self.threshold,
            "calibration_id": self.calibration_id,
        }


class EvidencePolicy:
    """Apply mode-specific thresholds only after an explicit calibration."""

    def __init__(
        self,
        thresholds: Mapping[str, float] | None = None,
        *,
        calibration_id: str = "",
    ) -> None:
        self._thresholds = dict(thresholds or {})
        self._calibration_id = calibration_id

    def evaluate(
        self,
        mode: RetrievalMode,
        chunks: list[dict],
        warnings: tuple[str, ...] | list[str],
    ) -> EvidenceDecision:
        score_name = RETRIEVAL_SCORE_FIELDS[mode]
        threshold = self._thresholds.get(mode)
        scores: list[float] = []
        for chunk in chunks:
            raw_score = chunk.get(score_name)
            if isinstance(raw_score, int | float) and not isinstance(raw_score, bool):
                score = float(raw_score)
                if math.isfinite(score):
                    scores.append(score)
        observed_score = max(scores) if scores else None

        if not chunks:
            sufficient = False
            reason = "no_retrieval_results"
        elif threshold is None:
            sufficient = True
            reason = "threshold_not_calibrated"
        elif mode == "hybrid+rerank" and any(
            warning in {"rerank_timeout", "rerank_unavailable"}
            for warning in warnings
        ):
            sufficient = True
            reason = "rerank_degraded"
        elif observed_score is None:
            sufficient = True
            reason = "score_unavailable"
        elif observed_score < threshold:
            sufficient = False
            reason = "score_below_threshold"
        else:
            sufficient = True
            reason = "score_at_or_above_threshold"

        return EvidenceDecision(
            sufficient=sufficient,
            reason=reason,
            score_name=score_name,
            observed_score=observed_score,
            threshold=threshold,
            calibration_id=self._calibration_id,
        )


@dataclass(frozen=True)
class RetrievalResult:
    """Observable result of one complete retrieval request."""

    query: str
    query_variants: list[str]
    candidates: list[dict]
    chunks: list[dict]
    warnings: tuple[str, ...] = ()
    index_version: str = LEGACY_INDEX_VERSION
    evidence: EvidenceDecision = field(
        default_factory=lambda: EvidenceDecision(
            sufficient=True,
            reason="not_evaluated",
            score_name="",
            observed_score=None,
            threshold=None,
            calibration_id="",
        )
    )


class KnowledgeSystem:
    """Own query rewriting, Dense/BM25 retrieval, RRF, and reranking."""

    def __init__(
        self,
        rewriter: QueryRewriter | None = None,
        retriever: CandidateRetriever | None = None,
        reranker: CandidateReranker | None = None,
        evidence_policy: EvidencePolicy | None = None,
    ) -> None:
        self._rewriter = rewriter or QueryRewriterNode()
        self._retriever = retriever or HybridRetrieverNode()
        self._reranker = reranker or RerankerNode()
        self._evidence_policy = evidence_policy or EvidencePolicy(
            settings.abstention_thresholds,
            calibration_id=settings.abstention_calibration_id,
        )

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

        rewrite_started = time.perf_counter()
        try:
            if mode in ("hybrid", "hybrid+rerank"):
                variants = await self._rewriter.rewrite(query)
            else:
                variants = [query]
        except Exception:
            tracer.add_span(
                None,
                "query_rewrite",
                (time.perf_counter() - rewrite_started) * 1000,
                status="error",
                error_code="query_rewrite_failed",
            )
            raise
        tracer.add_span(
            None,
            "query_rewrite",
            (time.perf_counter() - rewrite_started) * 1000,
            variants=len(variants),
            skipped=mode not in ("hybrid", "hybrid+rerank"),
        )

        retrieval_started = time.perf_counter()
        try:
            candidate_result = await asyncio.to_thread(
                self._retriever.search,
                variants,
                metadata_filter,
                mode,
                top_k,
            )
        except Exception:
            tracer.add_span(
                None,
                "candidate_retrieval",
                (time.perf_counter() - retrieval_started) * 1000,
                status="error",
                error_code="retrieval_failed",
                retrieval_mode=mode,
            )
            raise
        if isinstance(candidate_result, CandidateBatch):
            candidates = list(candidate_result)
            index_version = candidate_result.index_version
            dense_status = candidate_result.dense_status
            bm25_status = candidate_result.bm25_status
        else:
            candidates = candidate_result
            index_version = LEGACY_INDEX_VERSION
            dense_status = "not_requested"
            bm25_status = "not_requested"
        warnings: list[str] = []
        if mode in ("bm25_only", "hybrid", "hybrid+rerank"):
            bm25_warning = _BM25_WARNING_BY_STATUS.get(bm25_status)
            if bm25_warning is not None:
                warnings.append(bm25_warning)
        tracer.add_span(
            None,
            "candidate_retrieval",
            (time.perf_counter() - retrieval_started) * 1000,
            candidates=len(candidates),
            retrieval_mode=mode,
            dense_status=dense_status,
            bm25_status=bm25_status,
        )
        tracer.set_index_version(index_version)
        rerank_started = time.perf_counter()
        rerank_status = "ok"
        rerank_error = ""
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
                rerank_status = "degraded"
                rerank_error = "rerank_timeout"
                chunks = self._rank_without_reranker(candidates, top_k)
            except Exception as exc:
                logger.warning(
                    "Rerank failed: {}; using fusion order", type(exc).__name__
                )
                warnings.append("rerank_unavailable")
                rerank_status = "degraded"
                rerank_error = "rerank_unavailable"
                chunks = self._rank_without_reranker(candidates, top_k)
        else:
            chunks = self._rank_without_reranker(candidates, top_k)
        tracer.add_span(
            None,
            "rerank",
            (time.perf_counter() - rerank_started) * 1000,
            status=rerank_status,
            error_code=rerank_error,
            kept=len(chunks),
            skipped=mode != "hybrid+rerank" or not candidates,
        )

        evidence = self._evidence_policy.evaluate(mode, chunks, warnings)
        if not evidence.sufficient:
            chunks = []
            warnings.append("insufficient_evidence")
        elif evidence.reason == "score_unavailable":
            warnings.append("evidence_score_unavailable")
        tracer.add_span(
            None,
            "evidence_gate",
            sufficient=evidence.sufficient,
            reason=evidence.reason,
            score_name=evidence.score_name,
            observed_score=evidence.observed_score,
            threshold=evidence.threshold,
            calibration_id=evidence.calibration_id,
        )

        return RetrievalResult(
            query=query,
            query_variants=variants,
            candidates=candidates,
            chunks=chunks,
            warnings=tuple(warnings),
            index_version=index_version,
            evidence=evidence,
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
