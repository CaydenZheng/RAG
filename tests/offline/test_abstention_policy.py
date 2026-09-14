"""Regression tests for calibrated evidence gating and deterministic abstention."""

import asyncio
from types import SimpleNamespace

import pytest
from pydantic import ValidationError


@pytest.fixture(autouse=True)
def fixed_tokenizer(monkeypatch: pytest.MonkeyPatch) -> None:
    import tiktoken

    monkeypatch.setattr(
        tiktoken,
        "get_encoding",
        lambda name: SimpleNamespace(encode=lambda text: list(text)),
    )


def _chunk(score: float) -> dict:
    return {
        "chunk_id": "chunk-1",
        "text": "retrieved text",
        "metadata": {"source": "source.txt"},
        "dense_score": 0.7,
        "bm25_score": 3.0,
        "rrf_score": 0.03,
        "rerank_score": score,
    }


def test_uncalibrated_policy_only_rejects_empty_retrieval() -> None:
    from src.core.knowledge import EvidencePolicy

    policy = EvidencePolicy()

    no_results = policy.evaluate("hybrid+rerank", [], ())
    uncalibrated = policy.evaluate("hybrid+rerank", [_chunk(0.01)], ())

    assert no_results.sufficient is False
    assert no_results.reason == "no_retrieval_results"
    assert uncalibrated.sufficient is True
    assert uncalibrated.reason == "threshold_not_calibrated"


@pytest.mark.parametrize(
    ("mode", "threshold", "score_name"),
    [
        ("vector_only", 0.8, "dense_score"),
        ("bm25_only", 4.0, "bm25_score"),
        ("hybrid", 0.04, "rrf_score"),
        ("hybrid+rerank", 0.8, "rerank_score"),
    ],
)
def test_policy_uses_the_score_for_each_retrieval_mode(
    mode: str,
    threshold: float,
    score_name: str,
) -> None:
    from src.core.knowledge import EvidencePolicy

    decision = EvidencePolicy(
        {mode: threshold},
        calibration_id="sha256:calibration",
    ).evaluate(mode, [_chunk(0.7)], ())

    assert decision.sufficient is False
    assert decision.reason == "score_below_threshold"
    assert decision.score_name == score_name


def test_calibrated_policy_rejects_low_scores_but_not_degraded_reranking() -> None:
    from src.core.knowledge import EvidencePolicy

    policy = EvidencePolicy(
        {"hybrid+rerank": 0.5},
        calibration_id="sha256:calibration",
    )

    rejected = policy.evaluate("hybrid+rerank", [_chunk(0.49)], ())
    accepted = policy.evaluate("hybrid+rerank", [_chunk(0.5)], ())
    degraded = policy.evaluate(
        "hybrid+rerank",
        [_chunk(0.01)],
        ("rerank_timeout",),
    )

    assert rejected.sufficient is False
    assert rejected.reason == "score_below_threshold"
    assert rejected.observed_score == 0.49
    assert rejected.threshold == 0.5
    unavailable = policy.evaluate(
        "hybrid+rerank",
        [_chunk(float("nan"))],
        (),
    )

    assert accepted.sufficient is True
    assert degraded.sufficient is True
    assert degraded.reason == "rerank_degraded"
    assert unavailable.sufficient is True
    assert unavailable.reason == "score_unavailable"


def test_knowledge_system_keeps_scores_but_removes_rejected_chunks() -> None:
    from src.core.knowledge import EvidencePolicy, KnowledgeSystem

    class Rewriter:
        async def rewrite(self, query: str) -> list[str]:
            return [query]

    class Retriever:
        def search(self, queries, metadata_filter, mode, top_k):
            return [_chunk(0.2)]

    class Reranker:
        def rerank(self, query, candidates, top_k):
            return candidates

    system = KnowledgeSystem(
        rewriter=Rewriter(),
        retriever=Retriever(),
        reranker=Reranker(),
        evidence_policy=EvidencePolicy(
            {"hybrid+rerank": 0.5},
            calibration_id="sha256:calibration",
        ),
    )

    result = asyncio.run(system.retrieve("question", mode="hybrid+rerank"))

    assert result.chunks == []
    assert result.candidates[0]["rerank_score"] == 0.2
    assert result.evidence.reason == "score_below_threshold"
    assert result.warnings == ("insufficient_evidence",)


def test_retrieval_adapter_passes_the_evidence_decision() -> None:
    from src.core.knowledge import EvidenceDecision, RetrievalResult
    from src.orchestration.rag import KnowledgeRetrievalNode

    evidence = EvidenceDecision(
        sufficient=False,
        reason="score_below_threshold",
        score_name="rerank_score",
        observed_score=0.2,
        threshold=0.5,
        calibration_id="sha256:calibration",
    )
    result = RetrievalResult(
        query="question",
        query_variants=["question"],
        candidates=[_chunk(0.2)],
        chunks=[],
        warnings=("insufficient_evidence",),
        evidence=evidence,
    )
    shared: dict = {}

    asyncio.run(KnowledgeRetrievalNode().post_async(shared, (), result))

    assert shared["retrieved_chunks"] == []
    assert shared["abstain_reason"] == "score_below_threshold"
    assert shared["evidence"] == evidence.to_dict()


def test_stateless_abstention_skips_normal_and_streaming_llm_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.core import generation
    from src.core.generation import (
        INSUFFICIENT_EVIDENCE_ANSWER,
        AnswerInput,
        AnswerService,
    )
    from src.evaluation.metrics import is_abstention
    from src.infra import fallback

    async def fail_normal(*args, **kwargs):
        pytest.fail("normal LLM must not run for deterministic abstention")

    async def fail_stream(*args, **kwargs):
        pytest.fail("streaming LLM must not run for deterministic abstention")
        if False:
            yield ""

    monkeypatch.setattr(fallback, "chat_with_fallback_async", fail_normal)
    monkeypatch.setattr(generation.llm_client, "chat_stream_async", fail_stream)
    answer_input = AnswerInput(
        query="unknown question",
        context="No relevant documents found.",
        history=[],
        session_id="",
        valid_citation_refs=frozenset(),
        abstain_reason="score_below_threshold",
    )
    service = AnswerService()

    normal = asyncio.run(service.generate(answer_input))

    async def collect() -> list[str]:
        return [chunk async for chunk in service.stream(answer_input)]

    streamed = asyncio.run(collect())

    assert normal == INSUFFICIENT_EVIDENCE_ANSWER
    assert is_abstention(normal) is True
    assert streamed == [INSUFFICIENT_EVIDENCE_ANSWER]


def test_history_can_answer_without_reintroducing_rejected_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.core.generation import AnswerInput, AnswerService
    from src.infra import fallback

    calls: list[list[dict[str, str]]] = []

    async def answer_from_history(messages, **kwargs):
        calls.append(messages)
        return "Your previous question was about Ada."

    monkeypatch.setattr(fallback, "chat_with_fallback_async", answer_from_history)
    answer_input = AnswerInput(
        query="What did I just ask?",
        context="No relevant documents found.",
        history=[{"role": "user", "content": "Tell me about Ada."}],
        session_id="",
        valid_citation_refs=frozenset(),
        abstain_reason="no_retrieval_results",
    )

    answer = asyncio.run(AnswerService().generate(answer_input))

    assert answer == "Your previous question was about Ada."
    assert calls[0][1] == answer_input.history[0]
    assert "retrieved text" not in str(calls[0])


def test_abstention_thresholds_require_a_calibration_id() -> None:
    from config.settings import Settings

    with pytest.raises(ValidationError, match="ABSTENTION_CALIBRATION_ID"):
        Settings(
            _env_file=None,
            ABSTENTION_THRESHOLDS={"hybrid+rerank": 0.5},
            ABSTENTION_CALIBRATION_ID="",
        )

    with pytest.raises(ValidationError, match="do not match runtime models"):
        Settings(
            _env_file=None,
            ABSTENTION_THRESHOLDS={"hybrid+rerank": 0.5},
            ABSTENTION_CALIBRATION_ID="sha256:calibration",
            ABSTENTION_CALIBRATION_MODELS={
                "embedding": "different-embedding",
                "reranker": "different-reranker",
            },
        )

    configured = Settings(
        _env_file=None,
        ABSTENTION_THRESHOLDS={"hybrid+rerank": 0.5},
        ABSTENTION_CALIBRATION_ID="sha256:calibration",
        ABSTENTION_CALIBRATION_MODELS={
            "embedding": "BAAI/bge-base-en-v1.5",
            "reranker": "BAAI/bge-reranker-base",
        },
    )
    assert configured.abstention_thresholds == {"hybrid+rerank": 0.5}


def test_calibration_uses_human_verified_final_report() -> None:
    from src.evaluation.calibration import calibrate_report

    samples = [
        {
            "expected_behavior": behavior,
            "retrieval_candidates": [_chunk(score)],
        }
        for behavior, score in (
            ("abstain", 0.1),
            ("abstain", 0.2),
            ("abstain", 0.3),
            ("answer", 0.8),
            ("answer", 0.9),
        )
    ]
    report = {
        "reproducibility": {
            "data": {
                "split": "final",
                "catalog_version": "sha256:catalog",
            },
            "models": {
                "embedding": "embedding-model",
                "reranker": "reranker-model",
            },
        },
        "modes": {
            "hybrid+rerank": {
                "samples": samples,
            }
        },
    }

    calibration = calibrate_report(report)

    assert calibration["thresholds"] == {"hybrid+rerank": 0.8}
    assert calibration["modes"]["hybrid+rerank"]["correct_abstention_rate"] == 1.0
    assert calibration["modes"]["hybrid+rerank"]["false_abstention_rate"] == 0.0
    assert calibration["modes"]["hybrid+rerank"]["hard_answer_rate"] == 0.0
    assert calibration["calibration_id"].startswith("sha256:")

    report["reproducibility"]["data"]["split"] = "development"
    with pytest.raises(ValueError, match="human-verified final"):
        calibrate_report(report)

    report["reproducibility"]["data"]["split"] = "final"
    samples[0]["error_code"] = "evaluation_sample_failed"
    with pytest.raises(ValueError, match="failed samples"):
        calibrate_report(report)
