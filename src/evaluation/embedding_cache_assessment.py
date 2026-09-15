"""Privacy-preserving measurements for embedding cache decisions."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import statistics
import time
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path


def _normalize_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text)
    return " ".join(normalized.split()).casefold()


def query_statistics(queries: Iterable[str]) -> dict[str, int | float | str]:
    """Return aggregate repeat statistics without retaining query text."""
    normalized_queries = [_normalize_text(query) for query in queries]
    total_queries = len(normalized_queries)
    unique_queries = len(set(normalized_queries))
    duplicate_queries = total_queries - unique_queries
    return {
        "status": "measured",
        "total_queries": total_queries,
        "unique_queries": unique_queries,
        "duplicate_queries": duplicate_queries,
        "repeat_rate": duplicate_queries / total_queries if total_queries else 0.0,
    }


def session_query_statistics(db_path: Path) -> dict[str, int | float | str | None]:
    """Measure user turns in an existing session database without creating it."""
    if not db_path.is_file():
        return {
            "status": "not_measurable",
            "scope": "stored_user_turn_proxy",
            "limitation": "not_a_complete_embedding_workload",
            "reason": "sessions_db_missing",
            "total_queries": 0,
            "unique_queries": 0,
            "duplicate_queries": 0,
            "repeat_rate": None,
        }
    database_uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(database_uri, uri=True) as connection:
        queries = [
            row[0]
            for row in connection.execute(
                "SELECT content FROM sessions WHERE role = 'user'"
            )
        ]
    return {
        **query_statistics(queries),
        "scope": "stored_user_turn_proxy",
        "limitation": "not_a_complete_embedding_workload",
    }


def evaluation_query_statistics(
    report_directory: Path,
) -> dict[str, int | float | str | None]:
    """Measure embedding inputs from evaluation reports as a separate workload."""
    report_paths = sorted(report_directory.glob("*.json"))
    if not report_paths:
        return {
            "status": "not_measurable",
            "scope": "evaluation_only",
            "reason": "evaluation_reports_missing",
            "report_files": 0,
            "total_queries": 0,
            "unique_queries": 0,
            "duplicate_queries": 0,
            "repeat_rate": None,
        }

    queries: list[str] = []
    for report_path in report_paths:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        for mode in report.get("modes", {}).values():
            for sample in mode.get("samples", []):
                variants = sample.get("query_variants")
                if isinstance(variants, list) and variants:
                    queries.extend(
                        query for query in variants if isinstance(query, str)
                    )
                elif isinstance(sample.get("question"), str):
                    queries.append(sample["question"])

    return {
        **query_statistics(queries),
        "scope": "evaluation_only",
        "report_files": len(report_paths),
    }


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def document_statistics(
    current_texts: Iterable[str],
    *,
    existing_texts: Iterable[str] | None = None,
) -> dict[str, object]:
    """Compare exact chunk content without returning document text."""
    current_hashes = [_text_hash(text) for text in current_texts]
    unique_current = set(current_hashes)
    current_total = len(current_hashes)
    current_unique = len(unique_current)

    if existing_texts is None:
        existing_index: dict[str, object] = {
            "status": "not_measurable",
            "reason": "existing_index_unavailable",
            "total_chunks": 0,
            "unique_chunks": 0,
            "overlap_chunks": 0,
            "current_overlap_rate": None,
        }
    else:
        existing_hashes = [_text_hash(text) for text in existing_texts]
        unique_existing = set(existing_hashes)
        overlap_chunks = len(unique_current & unique_existing)
        existing_index = {
            "status": "measured",
            "total_chunks": len(existing_hashes),
            "unique_chunks": len(unique_existing),
            "overlap_chunks": overlap_chunks,
            "current_overlap_rate": (
                overlap_chunks / current_unique if current_unique else 0.0
            ),
        }

    return {
        "status": "measured",
        "current_chunks": {
            "total_chunks": current_total,
            "unique_chunks": current_unique,
            "duplicate_chunks": current_total - current_unique,
            "repeat_rate": (
                (current_total - current_unique) / current_total
                if current_total
                else 0.0
            ),
        },
        "existing_index": existing_index,
    }


_MANIFEST_IDENTITY_FIELDS = (
    "embedding_model",
    "embedding_revision",
    "normalization",
    "preprocessing",
    "vector_dimension",
)
_CACHE_KEY_FIELDS = (
    "embedding_model",
    "embedding_revision",
    "normalization",
    "preprocessing",
    "text_sha256",
    "vector_dimension",
)


def _missing_fields(identity: Mapping[str, object]) -> list[str]:
    return [
        field
        for field in _MANIFEST_IDENTITY_FIELDS
        if identity.get(field) in (None, "")
    ]


def vector_reuse_eligibility(
    *,
    current_identity: Mapping[str, object],
    existing_identity: Mapping[str, object] | None,
) -> dict[str, object]:
    """Require complete, matching provenance before vectors are called reusable."""
    existing = existing_identity or {}
    missing_current = _missing_fields(current_identity)
    missing_existing = _missing_fields(existing)
    mismatched_fields = [
        field
        for field in _MANIFEST_IDENTITY_FIELDS
        if field not in missing_current
        and field not in missing_existing
        and current_identity[field] != existing[field]
    ]
    eligible = not missing_current and not missing_existing and not mismatched_fields
    return {
        "status": "eligible" if eligible else "not_eligible",
        "missing_current_fields": missing_current,
        "missing_existing_fields": missing_existing,
        "mismatched_fields": mismatched_fields,
        "required_cache_key_fields": list(_CACHE_KEY_FIELDS),
    }


_BENCHMARK_TEXT = (
    "Synthetic embedding benchmark text. It contains no user or document content."
)


def benchmark_embedding(
    embedder: Callable[[str], Sequence[float]],
    *,
    warm_iterations: int = 5,
) -> dict[str, object]:
    """Measure cold and warm single-text latency using non-sensitive input."""
    if warm_iterations < 1:
        raise ValueError("warm_iterations must be at least 1")

    started_at = time.perf_counter()
    first_vector = embedder(_BENCHMARK_TEXT)
    cold_start_ms = (time.perf_counter() - started_at) * 1000

    warm_latency_ms: list[float] = []
    for _ in range(warm_iterations):
        started_at = time.perf_counter()
        vector = embedder(_BENCHMARK_TEXT)
        warm_latency_ms.append((time.perf_counter() - started_at) * 1000)
        if len(vector) != len(first_vector):
            raise ValueError("embedding dimension changed during benchmark")

    ordered_latency = sorted(warm_latency_ms)
    percentile_index = max(0, (95 * len(ordered_latency) + 99) // 100 - 1)
    return {
        "status": "measured",
        "input": "synthetic_text",
        "warm_iterations": warm_iterations,
        "vector_dimension": len(first_vector),
        "cold_start_ms": round(cold_start_ms, 3),
        "warm_mean_ms": round(statistics.fmean(warm_latency_ms), 3),
        "warm_p95_ms": round(ordered_latency[percentile_index], 3),
        "monetary_cost_usd": None,
        "cost_basis": "local_compute_unpriced",
    }


def build_assessment(
    *,
    production_queries: Mapping[str, object],
    evaluation_queries: Mapping[str, object],
    documents: Mapping[str, object],
    vector_reuse: Mapping[str, object],
    benchmark: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Combine measurements while keeping evaluation traffic out of query decisions."""
    rationale: list[str] = []
    production_measured = production_queries.get("status") == "measured"
    query_cache = "defer"
    if not production_measured:
        rationale.append("production_query_history_not_measurable")
    else:
        rationale.append("session_history_is_incomplete_query_proxy")

    existing_index = documents.get("existing_index", {})
    overlap_chunks = (
        existing_index.get("overlap_chunks", 0)
        if isinstance(existing_index, Mapping)
        else 0
    )
    safe_vector_reuse = vector_reuse.get("status") == "eligible"
    document_vector_reuse = (
        "implement" if safe_vector_reuse and overlap_chunks else "defer"
    )
    if not safe_vector_reuse:
        rationale.append("vector_provenance_not_eligible")
    elif not overlap_chunks:
        rationale.append("no_existing_chunk_overlap")

    if query_cache == "implement":
        overall = "implement"
    elif document_vector_reuse == "implement":
        overall = "document_vectors_only"
    else:
        overall = "defer"

    current_chunks = documents.get("current_chunks", {})
    document_duplicates = (
        current_chunks.get("duplicate_chunks", 0)
        if isinstance(current_chunks, Mapping)
        else 0
    )
    production_duplicates = (
        production_queries.get("duplicate_queries") if production_measured else None
    )
    warm_mean_ms = (
        benchmark.get("warm_mean_ms")
        if benchmark is not None and benchmark.get("status") == "measured"
        else None
    )

    def estimated_compute_ms(call_count: object) -> float | None:
        if not isinstance(warm_mean_ms, int | float) or not isinstance(
            call_count, int | float
        ):
            return None
        return round(warm_mean_ms * call_count, 3)

    safe_reusable_vectors = overlap_chunks if safe_vector_reuse else 0
    return {
        "schema_version": 1,
        "production_queries": dict(production_queries),
        "evaluation_queries": dict(evaluation_queries),
        "documents": dict(documents),
        "vector_reuse": dict(vector_reuse),
        "benchmark": dict(benchmark)
        if benchmark is not None
        else {"status": "not_run"},
        "decision_criteria": {
            "query_cache": {
                "required_observation": "complete_embedding_call_hit_rate",
                "value_threshold": "deployment_specific_not_defined",
            }
        },
        "embedding_economics": {
            "cost_basis": "local_compute_unpriced",
            "monetary_cost_usd": None,
            "production_query_duplicates": production_duplicates,
            "evaluation_query_duplicates": evaluation_queries.get("duplicate_queries"),
            "document_duplicate_chunks": document_duplicates,
            "legacy_text_overlap_chunks": overlap_chunks,
            "safe_legacy_vectors_reusable": safe_reusable_vectors,
            "estimated_compute_avoided_ms": {
                "production_query_proxy": estimated_compute_ms(production_duplicates),
                "evaluation_workload": estimated_compute_ms(
                    evaluation_queries.get("duplicate_queries")
                ),
                "duplicate_document_chunks": estimated_compute_ms(document_duplicates),
                "safe_legacy_vectors": estimated_compute_ms(safe_reusable_vectors),
            },
        },
        "decision": {
            "overall": overall,
            "query_cache": query_cache,
            "document_vector_reuse": document_vector_reuse,
            "rationale": rationale,
        },
    }
