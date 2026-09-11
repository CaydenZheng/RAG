"""Deterministic RAG metrics that require no model judge."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from pathlib import PurePosixPath
from typing import Any

_CITATION_PATTERN = re.compile(r"\[((?:\d+\s*,\s*)*\d+)\]")
_TOKEN_PATTERN = re.compile(r"[^\W_]+", re.UNICODE)
_ABSTENTION_MARKERS = (
    "cannot answer",
    "can't answer",
    "cannot determine",
    "unable to determine",
    "insufficient information",
    "not enough information",
    "no relevant",
    "does not provide",
    "doesn't provide",
    "not stated in the context",
    "无法回答",
    "无法确定",
    "信息不足",
    "没有足够",
    "未提供",
)


def _tokens(text: str) -> set[str]:
    return {
        token.lower()
        for token in _TOKEN_PATTERN.findall(text)
        if len(token) > 1
    }


def token_f1(answer: str, reference: str | None) -> float | None:
    """Return set-based token F1 for a deterministic answer-quality signal."""
    if reference is None:
        return None
    answer_tokens = _tokens(answer)
    reference_tokens = _tokens(reference)
    if not answer_tokens or not reference_tokens:
        return 0.0
    common = len(answer_tokens & reference_tokens)
    precision = common / len(answer_tokens)
    recall = common / len(reference_tokens)
    return 2 * precision * recall / (precision + recall) if common else 0.0


def is_abstention(answer: str) -> bool:
    """Recognize explicit insufficient-evidence responses."""
    lowered = answer.strip().lower()
    return not lowered or any(marker in lowered for marker in _ABSTENTION_MARKERS)


def extract_citation_refs(answer: str) -> tuple[int, ...]:
    """Extract unique inline references while preserving their first order."""
    refs: list[int] = []
    for match in _CITATION_PATTERN.finditer(answer):
        for raw_ref in match.group(1).split(","):
            ref = int(raw_ref.strip())
            if ref not in refs:
                refs.append(ref)
    return tuple(refs)


def _source_keys(value: str) -> frozenset[str]:
    normalized = value.replace("\\", "/").strip().lower().lstrip("./")
    if not normalized:
        return frozenset()
    keys = {normalized, PurePosixPath(normalized).name}
    if normalized.startswith("data/raw/"):
        keys.add(normalized.removeprefix("data/raw/"))
    return frozenset(keys)


def _expected_sources(sample: dict[str, Any]) -> tuple[frozenset[str], ...]:
    return tuple(
        _source_keys(str(source.get("path") or source.get("uri") or ""))
        for source in sample["source_documents"]
    )


def _result_source_keys(result: dict[str, Any]) -> frozenset[str]:
    metadata = result.get("metadata") or {}
    value = (
        metadata.get("source")
        or result.get("source")
        or metadata.get("path")
        or result.get("path")
        or ""
    )
    return _source_keys(str(value))


def _expected_source_index(
    result: dict[str, Any],
    expected_sources: tuple[frozenset[str], ...],
) -> int | None:
    result_keys = _result_source_keys(result)
    for index, source_keys in enumerate(expected_sources):
        if result_keys & source_keys:
            return index
    return None


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def retrieval_metrics(
    sample: dict[str, Any],
    chunks: list[dict[str, Any]],
    *,
    k_values: Iterable[int] = (1, 3, 5, 10),
) -> dict[str, float]:
    """Score unique expected source documents in ranked retrieval results."""
    expected_sources = _expected_sources(sample)
    ranked_sources = [
        _expected_source_index(chunk, expected_sources)
        for chunk in chunks
    ]
    metrics: dict[str, float] = {}
    for k in sorted(set(k_values)):
        seen = {
            source_index
            for source_index in ranked_sources[:k]
            if source_index is not None
        }
        metrics[f"recall@{k}"] = len(seen) / len(expected_sources)
        gains: list[int] = []
        accumulated: set[int] = set()
        for source_index in ranked_sources[:k]:
            relevant = source_index is not None and source_index not in accumulated
            gains.append(1 if relevant else 0)
            if source_index is not None:
                accumulated.add(source_index)
        dcg = sum(gain / math.log2(rank + 2) for rank, gain in enumerate(gains))
        ideal_hits = min(len(expected_sources), k)
        idcg = sum(1 / math.log2(rank + 2) for rank in range(ideal_hits))
        metrics[f"ndcg@{k}"] = dcg / idcg if idcg else 0.0

    first_relevant = next(
        (rank for rank, value in enumerate(ranked_sources, start=1) if value is not None),
        None,
    )
    metrics["mrr"] = 1 / first_relevant if first_relevant else 0.0
    return metrics


def citation_metrics(
    sample: dict[str, Any],
    answer: str,
    sources: list[dict[str, Any]],
) -> dict[str, float | int | None]:
    """Score whether inline citations point to and cover expected documents."""
    if sample["expected_behavior"] == "abstain":
        return {
            "citation_validity": None,
            "citation_correctness": None,
            "citation_completeness": None,
            "citation_count": len(extract_citation_refs(answer)),
            "invalid_citation_count": 0,
        }

    expected_sources = _expected_sources(sample)
    sources_by_ref = {
        int(source["ref"]): source
        for source in sources
        if isinstance(source.get("ref"), int)
    }
    cited_refs = extract_citation_refs(answer)
    valid_refs = [ref for ref in cited_refs if ref in sources_by_ref]
    relevant_source_indexes = {
        source_index
        for ref in valid_refs
        if (
            source_index := _expected_source_index(
                sources_by_ref[ref],
                expected_sources,
            )
        )
        is not None
    }
    relevant_citations = sum(
        _expected_source_index(sources_by_ref[ref], expected_sources) is not None
        for ref in valid_refs
    )
    citation_count = len(cited_refs)
    return {
        "citation_validity": len(valid_refs) / citation_count if citation_count else 0.0,
        "citation_correctness": (
            relevant_citations / citation_count if citation_count else 0.0
        ),
        "citation_completeness": (
            len(relevant_source_indexes) / len(expected_sources)
        ),
        "citation_count": citation_count,
        "invalid_citation_count": citation_count - len(valid_refs),
    }


def score_sample(
    sample: dict[str, Any],
    answer: str,
    chunks: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    *,
    k_values: Iterable[int] = (1, 3, 5, 10),
    error_code: str = "",
    judge_scores: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Compute all deterministic metrics for one completed runner result."""
    retrieval = retrieval_metrics(sample, chunks, k_values=k_values)
    citations = citation_metrics(sample, answer, sources)
    abstained = is_abstention(answer)
    lexical_score = token_f1(answer, sample.get("ground_truth"))
    max_recall = retrieval[
        f"recall@{max(k_values)}"
    ]
    if sample["expected_behavior"] == "abstain":
        succeeded = not error_code and abstained
    else:
        succeeded = (
            not error_code
            and bool(answer.strip())
            and not abstained
            and max_recall == 1.0
            and citations["citation_completeness"] == 1.0
            and lexical_score is not None
            and lexical_score >= 0.2
        )

    faithfulness = None
    relevancy = None
    if judge_scores is not None:
        faithfulness = judge_scores.get("faithfulness")
        relevancy = judge_scores.get("relevancy")
        succeeded = (
            succeeded
            and faithfulness is not None
            and relevancy is not None
            and faithfulness >= 0.5
            and relevancy >= 0.5
        )

    return {
        **retrieval,
        **citations,
        "answer_token_f1": lexical_score,
        "abstained": abstained,
        "faithfulness": faithfulness,
        "relevancy": relevancy,
        "task_success": succeeded,
    }


def _mean(results: list[dict[str, Any]], name: str) -> float | None:
    values = [
        result["metrics"][name]
        for result in results
        if result["metrics"].get(name) is not None
    ]
    return sum(values) / len(values) if values else None


def summarize_results(
    results: list[dict[str, Any]],
    *,
    k_values: Iterable[int] = (1, 3, 5, 10),
) -> dict[str, Any]:
    """Aggregate per-sample output into the report summary."""
    sample_count = len(results)
    latencies = [float(result["latency_ms"]) for result in results]
    errors = sum(bool(result.get("error_code")) for result in results)
    expected_abstentions = [
        result for result in results if result["expected_behavior"] == "abstain"
    ]
    expected_answers = [
        result for result in results if result["expected_behavior"] == "answer"
    ]
    predicted_abstentions = [
        result for result in results if result["metrics"]["abstained"]
    ]
    true_abstentions = sum(
        result["expected_behavior"] == "abstain"
        for result in predicted_abstentions
    )
    precision = (
        true_abstentions / len(predicted_abstentions)
        if predicted_abstentions
        else None
    )
    recall = (
        true_abstentions / len(expected_abstentions)
        if expected_abstentions
        else None
    )
    abstention_f1 = (
        2 * precision * recall / (precision + recall)
        if precision is not None and recall is not None and precision + recall
        else None
    )
    usage = {
        name: sum(int(result["usage"].get(name, 0)) for result in results)
        for name in ("prompt_tokens", "completion_tokens", "total_tokens")
    }
    usage["reported_sample_rate"] = (
        sum(bool(result["usage"].get("reported")) for result in results) / sample_count
        if sample_count
        else 0.0
    )
    costs = [result.get("cost_usd") for result in results]
    total_cost = (
        sum(float(cost) for cost in costs)
        if costs and all(cost is not None for cost in costs)
        else None
    )
    by_task_type: dict[str, dict[str, float | int]] = {}
    task_types = sorted(
        {
            task_type
            for result in results
            for task_type in result["task_types"]
        }
    )
    for task_type in task_types:
        task_results = [
            result for result in results if task_type in result["task_types"]
        ]
        by_task_type[task_type] = {
            "samples": len(task_results),
            "task_success_rate": sum(
                result["metrics"]["task_success"] for result in task_results
            )
            / len(task_results),
        }

    metrics = {
        "mrr": _mean(results, "mrr"),
        "citation_validity": _mean(results, "citation_validity"),
        "citation_correctness": _mean(results, "citation_correctness"),
        "citation_completeness": _mean(results, "citation_completeness"),
        "faithfulness": _mean(results, "faithfulness"),
        "relevancy": _mean(results, "relevancy"),
    }
    for k in sorted(set(k_values)):
        metrics[f"recall@{k}"] = _mean(results, f"recall@{k}")
        metrics[f"ndcg@{k}"] = _mean(results, f"ndcg@{k}")

    return {
        "sample_count": sample_count,
        "metrics": metrics,
        "task_success_rate": (
            sum(result["metrics"]["task_success"] for result in results) / sample_count
            if sample_count
            else 0.0
        ),
        "no_answer": {
            "expected_unanswerable": len(expected_abstentions),
            "expected_answerable": len(expected_answers),
            "abstention_precision": precision,
            "abstention_recall": recall,
            "abstention_f1": abstention_f1,
            "answerable_response_rate": (
                sum(not result["metrics"]["abstained"] for result in expected_answers)
                / len(expected_answers)
                if expected_answers
                else None
            ),
        },
        "latency_ms": {
            "p50": _percentile(latencies, 0.50),
            "p95": _percentile(latencies, 0.95),
        },
        "error_rate": errors / sample_count if sample_count else 0.0,
        "usage": usage,
        "cost_usd": total_cost,
        "by_task_type": by_task_type,
    }
