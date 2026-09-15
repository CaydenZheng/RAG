"""Latency measurements for the synchronous ContextBuilder execution seam."""

from __future__ import annotations

import asyncio
import math
import statistics
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any


def _percentile(values: Sequence[float], quantile: float) -> float:
    if not values:
        raise ValueError("latency samples cannot be empty")
    if not 0 <= quantile <= 1:
        raise ValueError("quantile must be between zero and one")
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def _latency_summary(values: Sequence[float]) -> dict[str, int | float]:
    return {
        "samples": len(values),
        "mean_ms": round(statistics.fmean(values), 3),
        "p50_ms": round(_percentile(values, 0.50), 3),
        "p95_ms": round(_percentile(values, 0.95), 3),
        "max_ms": round(max(values), 3),
    }


def measure_sync_latencies(
    run_sync: Callable[[], Any],
    *,
    iterations: int,
) -> list[float]:
    """Measure one synchronous call repeatedly in milliseconds."""
    if iterations < 1:
        raise ValueError("iterations must be at least one")
    latencies: list[float] = []
    for _ in range(iterations):
        started_at = time.perf_counter()
        run_sync()
        latencies.append((time.perf_counter() - started_at) * 1000)
    return latencies


async def measure_event_loop_stalls(
    run_sync: Callable[[], Any],
    *,
    iterations: int,
) -> list[float]:
    """Measure how long a ready callback waits behind one synchronous call."""
    if iterations < 1:
        raise ValueError("iterations must be at least one")
    loop = asyncio.get_running_loop()
    stalls: list[float] = []
    for _ in range(iterations):
        completed = asyncio.Event()
        scheduled_at = time.perf_counter()

        def mark_ready() -> None:
            stalls.append((time.perf_counter() - scheduled_at) * 1000)
            completed.set()

        loop.call_soon(mark_ready)
        run_sync()
        await completed.wait()
    return stalls


def build_assessment(
    *,
    workload: Mapping[str, object],
    iterations: int,
    history_read_ms: Sequence[float],
    token_count_call_ms: Sequence[float],
    token_count_total_ms: Sequence[float],
    context_builder_total_ms: Sequence[float],
    event_loop_stall_ms: Sequence[float],
    stall_threshold_ms: float,
) -> dict[str, object]:
    """Summarize component latency and screen for meaningful loop blocking."""
    if iterations < 1:
        raise ValueError("iterations must be at least one")
    if stall_threshold_ms <= 0:
        raise ValueError("stall threshold must be positive")

    metrics = {
        "history_read": _latency_summary(history_read_ms),
        "token_count_call": _latency_summary(token_count_call_ms),
        "token_count_total": _latency_summary(token_count_total_ms),
        "context_builder_total": _latency_summary(context_builder_total_ms),
        "event_loop_stall": _latency_summary(event_loop_stall_ms),
    }
    measured_stall_p95 = metrics["event_loop_stall"]["p95_ms"]
    should_offload = measured_stall_p95 >= stall_threshold_ms
    return {
        "schema_version": 1,
        "iterations": iterations,
        "workload": dict(workload),
        "metrics": metrics,
        "decision": {
            "async_conversion": "implement_offload" if should_offload else "defer",
            "event_loop_stall_threshold_ms": stall_threshold_ms,
            "rationale": (
                "measured_p95_at_or_above_local_screening_threshold"
                if should_offload
                else "measured_p95_below_local_screening_threshold"
            ),
        },
    }
