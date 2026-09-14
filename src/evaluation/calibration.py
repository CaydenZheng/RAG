"""Calibrate evidence-score thresholds from a human-verified final report."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

from src.core.knowledge import RETRIEVAL_SCORE_FIELDS, RetrievalMode


def _top_score(sample: dict[str, Any], score_name: str) -> float | None:
    candidates = sample.get("retrieval_candidates") or sample.get(
        "retrieved_chunks", []
    )
    scores = [
        float(candidate[score_name])
        for candidate in candidates
        if isinstance(candidate.get(score_name), int | float)
        and not isinstance(candidate.get(score_name), bool)
        and math.isfinite(float(candidate[score_name]))
    ]
    return max(scores) if scores else None


def _is_comparable(sample: dict[str, Any], mode: RetrievalMode) -> bool:
    if mode != "hybrid+rerank":
        return True
    warnings = set(sample.get("warnings", []))
    return not warnings.intersection({"rerank_timeout", "rerank_unavailable"})


def calibrate_mode(
    samples: list[dict[str, Any]],
    mode: RetrievalMode,
) -> dict[str, Any]:
    """Choose the safest threshold among equally accurate score boundaries."""
    comparable = [sample for sample in samples if _is_comparable(sample, mode)]
    score_name = RETRIEVAL_SCORE_FIELDS[mode]
    behaviors = {sample.get("expected_behavior") for sample in comparable}
    if not behaviors <= {"answer", "abstain"}:
        raise ValueError(f"{mode} contains invalid expected_behavior values")
    observations = [
        (sample["expected_behavior"], _top_score(sample, score_name))
        for sample in comparable
    ]
    abstain_count = sum(behavior == "abstain" for behavior, _ in observations)
    answer_count = sum(behavior == "answer" for behavior, _ in observations)
    if abstain_count < 3:
        raise ValueError(f"{mode} requires at least 3 abstain samples")
    if answer_count < 1:
        raise ValueError(f"{mode} requires at least 1 answer sample")

    finite_scores = sorted({score for _, score in observations if score is not None})
    if not finite_scores:
        raise ValueError(f"{mode} has no comparable {score_name} observations")
    thresholds = list(finite_scores)
    upper_boundary = math.nextafter(finite_scores[-1], math.inf)
    if math.isfinite(upper_boundary):
        thresholds.append(upper_boundary)

    candidates: list[dict[str, Any]] = []
    for threshold in thresholds:
        correct_abstentions = 0
        false_abstentions = 0
        hard_answers = 0
        correct_answers = 0
        for behavior, score in observations:
            predicted_abstain = score is None or score < threshold
            if behavior == "abstain" and predicted_abstain:
                correct_abstentions += 1
            elif behavior == "answer" and predicted_abstain:
                false_abstentions += 1
            elif behavior == "abstain":
                hard_answers += 1
            else:
                correct_answers += 1

        correct_abstention_rate = correct_abstentions / abstain_count
        false_abstention_rate = false_abstentions / answer_count
        hard_answer_rate = hard_answers / abstain_count
        correct_answer_rate = correct_answers / answer_count
        balanced_accuracy = (
            correct_abstention_rate + correct_answer_rate
        ) / 2
        candidates.append(
            {
                "threshold": threshold,
                "balanced_accuracy": balanced_accuracy,
                "correct_abstention_rate": correct_abstention_rate,
                "false_abstention_rate": false_abstention_rate,
                "hard_answer_rate": hard_answer_rate,
            }
        )

    selected = max(
        candidates,
        key=lambda candidate: (
            candidate["balanced_accuracy"],
            -candidate["hard_answer_rate"],
            -candidate["false_abstention_rate"],
            candidate["threshold"],
        ),
    )
    return {
        "score_name": score_name,
        "sample_count": len(observations),
        "abstain_sample_count": abstain_count,
        "answer_sample_count": answer_count,
        "excluded_degraded_samples": len(samples) - len(comparable),
        **selected,
    }


def calibrate_report(report: dict[str, Any]) -> dict[str, Any]:
    """Build auditable runtime thresholds from a final-split evaluation report."""
    reproducibility = report.get("reproducibility") or {}
    data = reproducibility.get("data") or {}
    if data.get("split") != "final":
        raise ValueError("calibration requires a human-verified final report")

    mode_results = report.get("modes") or {}
    failed_samples = [
        sample
        for mode_result in mode_results.values()
        for sample in (mode_result.get("samples") or [])
        if sample.get("error_code")
    ]
    if failed_samples:
        raise ValueError("calibration report contains failed samples")

    mode_calibrations: dict[str, dict[str, Any]] = {}
    for raw_mode, mode_result in mode_results.items():
        if raw_mode not in RETRIEVAL_SCORE_FIELDS:
            raise ValueError(f"unsupported retrieval mode in report: {raw_mode}")
        mode = raw_mode
        mode_calibrations[mode] = calibrate_mode(
            list(mode_result.get("samples") or []),
            mode,
        )
    if not mode_calibrations:
        raise ValueError("calibration report contains no retrieval modes")

    canonical_report = json.dumps(
        report,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    calibration_id = "sha256:" + hashlib.sha256(canonical_report).hexdigest()
    return {
        "schema_version": 1,
        "calibration_id": calibration_id,
        "catalog_version": data.get("catalog_version", "unavailable"),
        "models": reproducibility.get("models") or {},
        "thresholds": {
            mode: calibration["threshold"]
            for mode, calibration in mode_calibrations.items()
        },
        "modes": mode_calibrations,
    }
