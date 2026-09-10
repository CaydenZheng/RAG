"""Fast offline checks for the RAG evaluation runner and metrics."""

from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path

import pytest

from src.core.knowledge import RetrievalResult
from src.evaluation.datasets import DatasetCatalog, EvaluationDataset
from src.evaluation.metrics import (
    citation_metrics,
    is_abstention,
    retrieval_metrics,
    score_sample,
    summarize_results,
)
from src.evaluation.runner import (
    EvaluationConfig,
    EvaluationRunner,
    GeneratedAnswer,
    write_report,
)


def _sample(*, behavior: str = "answer") -> dict:
    unanswerable = behavior == "abstain"
    return {
        "id": "sample-001",
        "question": "When was Ada Lovelace born?",
        "ground_truth": None if unanswerable else "Ada Lovelace was born in 1815.",
        "expected_behavior": behavior,
        "task_types": ["unanswerable"] if unanswerable else ["factual", "temporal"],
        "split": "development",
        "source_documents": [
            {"path": "data/raw/wiki/Ada Lovelace.txt", "sha256": "0" * 64}
        ],
    }


def _chunk(source: str, chunk_id: str) -> dict:
    return {
        "chunk_id": chunk_id,
        "text": "Ada Lovelace was born in 1815.",
        "metadata": {"source": source},
        "rerank_score": 0.9,
    }


def test_retrieval_metrics_count_unique_expected_documents() -> None:
    sample = _sample()
    sample["source_documents"].append(
        {"path": "data/raw/wiki/Abraham Lincoln.txt", "sha256": "1" * 64}
    )
    chunks = [
        _chunk("wiki/Ada Lovelace.txt", "ada-1"),
        _chunk("wiki/Ada Lovelace.txt", "ada-2"),
        _chunk("wiki/Abraham Lincoln.txt", "lincoln-1"),
    ]

    metrics = retrieval_metrics(sample, chunks, k_values=(1, 3))

    assert metrics["recall@1"] == 0.5
    assert metrics["recall@3"] == 1.0
    assert metrics["mrr"] == 1.0
    expected_ndcg = (1 + 1 / math.log2(4)) / (1 + 1 / math.log2(3))
    assert metrics["ndcg@3"] == pytest.approx(expected_ndcg)


def test_citation_metrics_reject_irrelevant_and_missing_sources() -> None:
    sample = _sample()
    sample["source_documents"].append(
        {"path": "data/raw/wiki/Abraham Lincoln.txt", "sha256": "1" * 64}
    )
    sources = [
        {"ref": 1, "source": "wiki/Ada Lovelace.txt"},
        {"ref": 2, "source": "wiki/Unrelated.txt"},
    ]

    metrics = citation_metrics(sample, "Answer [1, 2] and forged [99].", sources)

    assert metrics == {
        "citation_validity": 2 / 3,
        "citation_correctness": 1 / 3,
        "citation_completeness": 0.5,
        "citation_count": 3,
        "invalid_citation_count": 1,
    }


def test_no_answer_and_summary_metrics_are_deterministic() -> None:
    answerable = _sample()
    answer_result = {
        **answerable,
        "metrics": score_sample(
            answerable,
            "Ada Lovelace was born in 1815 [1].",
            [_chunk("wiki/Ada Lovelace.txt", "ada-1")],
            [{"ref": 1, "source": "wiki/Ada Lovelace.txt"}],
            k_values=(1,),
        ),
        "latency_ms": 10,
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
            "reported": True,
        },
        "cost_usd": 0.01,
        "error_code": "",
    }
    unanswerable = _sample(behavior="abstain")
    abstain_result = {
        **unanswerable,
        "metrics": score_sample(
            unanswerable,
            "The context does not provide enough information.",
            [_chunk("wiki/Ada Lovelace.txt", "ada-1")],
            [],
            k_values=(1,),
        ),
        "latency_ms": 30,
        "usage": {
            "prompt_tokens": 8,
            "completion_tokens": 4,
            "total_tokens": 12,
            "reported": True,
        },
        "cost_usd": 0.02,
        "error_code": "",
    }

    summary = summarize_results([answer_result, abstain_result], k_values=(1,))

    assert is_abstention(abstain_result["answer"] if "answer" in abstain_result else "insufficient information")
    assert summary["task_success_rate"] == 1.0
    assert summary["no_answer"]["abstention_precision"] == 1.0
    assert summary["no_answer"]["abstention_recall"] == 1.0
    assert summary["latency_ms"] == {"p50": 20.0, "p95": 29.0}
    assert summary["error_rate"] == 0.0
    assert summary["usage"]["total_tokens"] == 27
    assert summary["cost_usd"] == pytest.approx(0.03)


def test_runner_calls_unified_core_and_applies_opt_in_judge(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from src.evaluation import runner as runner_module

    calls: list[tuple] = []

    class FakeKnowledgeSystem:
        async def retrieve(self, query: str, **kwargs) -> RetrievalResult:
            calls.append(("retrieve", query, kwargs))
            chunk = _chunk("wiki/Ada Lovelace.txt", "ada-1")
            return RetrievalResult(
                query=query,
                query_variants=[query],
                candidates=[chunk],
                chunks=[chunk],
                index_version="index-v1",
            )

    class FakeAnswerGenerator:
        async def generate(self, query, chunks, index_version) -> GeneratedAnswer:
            calls.append(("generate", query, len(chunks), index_version))
            return GeneratedAnswer(
                answer="Ada Lovelace was born in 1815 [1].",
                context="context",
                sources=[{"ref": 1, "source": "wiki/Ada Lovelace.txt"}],
            )

    class FakeJudge:
        name = "fake-judge"

        def score(self, results):
            calls.append(("judge", len(results)))
            return {
                result["sample_id"]: {"faithfulness": 0.9, "relevancy": 0.8}
                for result in results
            }

    monkeypatch.setattr(
        runner_module,
        "capture_reproducibility",
        lambda catalog, config, project_root: {"captured": True},
    )
    dataset = EvaluationDataset(
        path=tmp_path / "development.json",
        records=(_sample(),),
        version="sha256:data",
    )
    catalog = DatasetCatalog(
        path=tmp_path / "manifest.json",
        datasets=(dataset,),
        version="sha256:catalog",
    )
    runner = EvaluationRunner(
        knowledge_system=FakeKnowledgeSystem(),
        answer_generator=FakeAnswerGenerator(),
        judge=FakeJudge(),
        project_root=tmp_path,
    )

    report = asyncio.run(
        runner.run(
            catalog,
            EvaluationConfig(
                split="development",
                modes=("hybrid+rerank",),
                top_k=5,
                metric_k=(1, 5),
            ),
        )
    )

    result = report["modes"]["hybrid+rerank"]["samples"][0]
    assert [call[0] for call in calls] == ["retrieve", "generate", "judge"]
    assert result["index_version"] == "index-v1"
    assert result["metrics"]["faithfulness"] == 0.9
    assert result["metrics"]["relevancy"] == 0.8
    assert result["metrics"]["task_success"]
    assert report["reproducibility"] == {"captured": True}


def test_report_write_is_complete_json(tmp_path: Path) -> None:
    report = {"schema_version": 1, "samples": [{"answer": "完整"}]}
    destination = write_report(tmp_path / "run.json", report)

    assert json.loads(destination.read_text(encoding="utf-8")) == report
    assert not list(tmp_path.glob("*.tmp"))
