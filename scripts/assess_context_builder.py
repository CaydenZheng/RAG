"""Measure synchronous ContextBuilder latency without changing runtime behavior."""

from __future__ import annotations

import argparse
import asyncio
import gc
import json
import os
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from loguru import logger

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_ITERATIONS = 200
DEFAULT_STALL_THRESHOLD_MS = 10.0
SYNTHETIC_TEXT_CHARS = 512


def _synthetic_text(label: str) -> str:
    sentence = f"{label} synthetic content for local latency measurement only. "
    repeats = SYNTHETIC_TEXT_CHARS // len(sentence) + 1
    return (sentence * repeats)[:SYNTHETIC_TEXT_CHARS]


def _chunks(count: int) -> list[dict[str, object]]:
    return [
        {
            "chunk_id": f"synthetic-chunk-{index}",
            "doc_id": "synthetic-document",
            "text": _synthetic_text(f"Evidence {index}"),
            "rerank_score": 1.0 - index / max(count, 1),
            "metadata": {
                "source": "synthetic.md",
                "chunk_index": index,
            },
        }
        for index in range(count)
    ]


def _run_scenario(
    *,
    generation: Any,
    store_type: Any,
    database_path: Path,
    chunk_count: int,
    iterations: int,
    stall_threshold_ms: float,
) -> dict[str, object]:
    from src.evaluation.context_builder_assessment import (
        build_assessment,
        measure_event_loop_stalls,
        measure_sync_latencies,
    )

    store = store_type(str(database_path))
    session_id = f"assessment-{chunk_count}"
    for index in range(10):
        store.append_exchange(
            session_id,
            _synthetic_text(f"Question {index}"),
            _synthetic_text(f"Answer {index}"),
        )

    history_read_ms: list[float] = []
    token_count_call_ms: list[float] = []
    token_count_total_ms: list[float] = []
    original_store = generation.session_store
    original_count_tokens = generation.count_tokens

    class MeasuredHistoryStore:
        def get_recent_history(
            self, requested_session_id: str, limit: int
        ) -> list[dict[str, str]]:
            started_at = time.perf_counter()
            history = store.get_recent_history(requested_session_id, limit)
            history_read_ms.append((time.perf_counter() - started_at) * 1000)
            return history

    def measured_count_tokens(text: str) -> int:
        started_at = time.perf_counter()
        count = original_count_tokens(text)
        token_count_call_ms.append((time.perf_counter() - started_at) * 1000)
        return count

    generation.session_store = MeasuredHistoryStore()
    generation.count_tokens = measured_count_tokens
    builder = generation.ContextBuilderNode()
    inputs = (_chunks(chunk_count), session_id)

    def run_once() -> None:
        first_token_sample = len(token_count_call_ms)
        builder.exec(inputs)
        token_count_total_ms.append(sum(token_count_call_ms[first_token_sample:]))

    try:
        run_once()
        history_read_ms.clear()
        token_count_call_ms.clear()
        token_count_total_ms.clear()
        total_ms = measure_sync_latencies(run_once, iterations=iterations)
        event_loop_stall_ms = asyncio.run(
            measure_event_loop_stalls(run_once, iterations=iterations)
        )
    finally:
        generation.session_store = original_store
        generation.count_tokens = original_count_tokens

    return build_assessment(
        workload={
            "synthetic": True,
            "history_turns_stored": 20,
            "history_turns_loaded": builder.HISTORY_TURNS,
            "chunks": chunk_count,
            "text_chars_per_turn_or_chunk": SYNTHETIC_TEXT_CHARS,
        },
        iterations=iterations,
        history_read_ms=history_read_ms,
        token_count_call_ms=token_count_call_ms,
        token_count_total_ms=token_count_total_ms,
        context_builder_total_ms=total_ms,
        event_loop_stall_ms=event_loop_stall_ms,
        stall_threshold_ms=stall_threshold_ms,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure whether ContextBuilder should move off the event loop."
    )
    parser.add_argument("--iterations", type=int, default=DEFAULT_ITERATIONS)
    parser.add_argument(
        "--stall-threshold-ms",
        type=float,
        default=DEFAULT_STALL_THRESHOLD_MS,
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.iterations < 1:
        parser.error("--iterations must be at least one")
    if args.stall_threshold_ms <= 0:
        parser.error("--stall-threshold-ms must be positive")

    logger.remove()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
        temporary_path = Path(temporary)
        os.environ["CHROMA_PERSIST_DIR"] = str(temporary_path / "chroma")
        os.environ["CACHE_DB_PATH"] = str(temporary_path / "cache.db")

        from config.settings import settings
        from src.core import generation
        from src.core.knowledge import (
            DEFAULT_RETRIEVAL_TOP_K,
            MAX_RETRIEVAL_TOP_K,
        )
        from src.infra.session_store import SessionStore

        scenarios = {
            "default_retrieval": _run_scenario(
                generation=generation,
                store_type=SessionStore,
                database_path=temporary_path / "default-sessions.db",
                chunk_count=DEFAULT_RETRIEVAL_TOP_K,
                iterations=args.iterations,
                stall_threshold_ms=args.stall_threshold_ms,
            ),
            "maximum_supported_retrieval": _run_scenario(
                generation=generation,
                store_type=SessionStore,
                database_path=temporary_path / "maximum-sessions.db",
                chunk_count=MAX_RETRIEVAL_TOP_K,
                iterations=args.iterations,
                stall_threshold_ms=args.stall_threshold_ms,
            ),
        }
        should_offload = any(
            scenario["decision"]["async_conversion"] == "implement_offload"
            for scenario in scenarios.values()
        )
        report = {
            "schema_version": 1,
            "environment": {
                "python": sys.version.split()[0],
                "max_context_tokens": settings.max_context_tokens,
            },
            "method": {
                "input": "synthetic_text_only",
                "event_loop_probe": "call_soon_callback_delayed_by_sync_exec",
                "screening_threshold_ms": args.stall_threshold_ms,
                "threshold_scope": "local_engineering_screen_not_production_slo",
            },
            "scenarios": scenarios,
            "decision": {
                "async_conversion": ("implement_offload" if should_offload else "defer")
            },
            "limitations": [
                "single_process_local_measurement",
                "does_not_model_sqlite_lock_contention",
                "synthetic_text_is_not_production_traffic",
            ],
        }
        encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if args.output is None:
            print(encoded, end="")
        else:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(encoded, encoding="utf-8")

        del scenarios
        gc.collect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
