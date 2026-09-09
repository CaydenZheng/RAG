"""Regression coverage for JSONL trace records, with no repository log writes."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_trace_records_success_and_failure(
    isolated_runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.infra import tracer as trace_module

    timestamps = iter([100.0, 101.25, 200.0, 200.75])
    monkeypatch.setattr(
        trace_module, "time", SimpleNamespace(time=lambda: next(timestamps))
    )
    tracer = trace_module.TraceLogger()

    success = tracer.start_trace("success-id", "What is RRF?")
    tracer.add_span(success, "rewrite", latency_ms=20.125, variants=2)
    tracer.add_span(success, "generator", latency_ms=30.5, answer_chars=6)
    tracer.finish_trace(success, answer="融合排序算法", sources=1)

    failure = tracer.start_trace("failure-id", "Explain a timeout")
    tracer.add_span(failure, "rewrite", latency_ms=10.0, variants=1)
    tracer.finish_trace(failure, answer="", error="LLM timeout")

    trace_path = isolated_runtime / "logs/traces.jsonl"
    records = [
        json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()
    ]
    assert records == [
        {
            "query_id": "success-id",
            "query": "What is RRF?",
            "total_ms": 1250.0,
            "nodes": [
                {"node": "rewrite", "latency_ms": 20.12, "variants": 2},
                {"node": "generator", "latency_ms": 30.5, "answer_chars": 6},
            ],
            "answer_len": 6,
            "sources": 1,
        },
        {
            "query_id": "failure-id",
            "query": "Explain a timeout",
            "total_ms": 750.0,
            "nodes": [{"node": "rewrite", "latency_ms": 10.0, "variants": 1}],
            "answer_len": 0,
            "error": "LLM timeout",
        },
    ]
