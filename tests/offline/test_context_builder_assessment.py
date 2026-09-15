"""Offline checks for ContextBuilder event-loop latency assessment."""

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_event_loop_probe_detects_context_builder_history_blocking(
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tiktoken

    monkeypatch.setattr(
        tiktoken,
        "get_encoding",
        lambda name: SimpleNamespace(encode=lambda text: list(text)),
    )

    from src.core import generation
    from src.evaluation.context_builder_assessment import (
        measure_event_loop_stalls,
    )

    class DelayedHistoryStore:
        def get_recent_history(
            self, session_id: str, limit: int
        ) -> list[dict[str, str]]:
            time.sleep(0.02)
            return []

    monkeypatch.setattr(generation, "session_store", DelayedHistoryStore())
    builder = generation.ContextBuilderNode()
    stalls = asyncio.run(
        measure_event_loop_stalls(
            lambda: builder.exec(([], "session")),
            iterations=3,
        )
    )

    assert len(stalls) == 3
    assert min(stalls) >= 15.0


def test_assessment_records_component_percentiles_and_decision() -> None:
    from src.evaluation.context_builder_assessment import build_assessment

    assessment = build_assessment(
        workload={"history_turns_loaded": 6, "chunks": 5},
        iterations=4,
        history_read_ms=[0.1, 0.2, 0.3, 0.4],
        token_count_call_ms=[0.01, 0.02, 0.03, 0.04],
        token_count_total_ms=[0.2, 0.3, 0.4, 0.5],
        context_builder_total_ms=[0.5, 0.6, 0.7, 0.8],
        event_loop_stall_ms=[0.6, 0.7, 0.8, 0.9],
        stall_threshold_ms=10.0,
    )

    assert assessment["schema_version"] == 1
    assert assessment["metrics"]["history_read"]["p50_ms"] == 0.25
    assert assessment["metrics"]["history_read"]["p95_ms"] == 0.385
    assert assessment["metrics"]["token_count_call"]["p95_ms"] == 0.038
    assert assessment["metrics"]["token_count_total"]["p95_ms"] == 0.485
    assert assessment["decision"] == {
        "async_conversion": "defer",
        "event_loop_stall_threshold_ms": 10.0,
        "rationale": "measured_p95_below_local_screening_threshold",
    }


def test_assessment_recommends_offload_above_threshold() -> None:
    from src.evaluation.context_builder_assessment import build_assessment

    assessment = build_assessment(
        workload={"history_turns_loaded": 6, "chunks": 5},
        iterations=2,
        history_read_ms=[12.0, 13.0],
        token_count_call_ms=[0.1, 0.1],
        token_count_total_ms=[0.2, 0.2],
        context_builder_total_ms=[12.5, 13.5],
        event_loop_stall_ms=[12.6, 13.6],
        stall_threshold_ms=10.0,
    )

    assert assessment["decision"]["async_conversion"] == "implement_offload"
    assert assessment["decision"]["rationale"] == (
        "measured_p95_at_or_above_local_screening_threshold"
    )
