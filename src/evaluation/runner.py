"""Reproducible RAG evaluation through the production retrieval and answer seams."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import json
import os
import platform
import random
import subprocess
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import yaml

from config.settings import settings
from src.core.generation import AnswerInput, ContextBuilderNode, answer_service
from src.core.knowledge import (
    DEFAULT_RETRIEVAL_MODE,
    KnowledgeSystem,
    RetrievalMode,
    validate_retrieval_mode,
    validate_retrieval_top_k,
)
from src.evaluation.datasets import DatasetCatalog
from src.evaluation.metrics import score_sample, summarize_results
from src.infra.index_catalog import index_catalog
from src.infra.tracer import tracer
from src.llm.cache_context import scoped_cache_identity

DEFAULT_METRIC_K = (1, 3, 5, 10)


@dataclass(frozen=True, slots=True)
class EvaluationConfig:
    """All choices that can change one evaluation run."""

    split: str
    modes: tuple[RetrievalMode, ...] = (DEFAULT_RETRIEVAL_MODE,)
    top_k: int = 5
    metric_k: tuple[int, ...] = DEFAULT_METRIC_K
    seed: int = 20260910
    sample_limit: int | None = None
    input_cost_per_million: float | None = None
    output_cost_per_million: float | None = None

    def __post_init__(self) -> None:
        if self.split not in {"development", "final"}:
            raise ValueError("split must be development or final")
        if not self.modes or len(self.modes) != len(set(self.modes)):
            raise ValueError("modes must be non-empty and unique")
        for mode in self.modes:
            validate_retrieval_mode(mode)
        validate_retrieval_top_k(self.top_k)
        if not self.metric_k or any(k < 1 for k in self.metric_k):
            raise ValueError("metric_k must contain positive integers")
        if self.sample_limit is not None and self.sample_limit < 1:
            raise ValueError("sample_limit must be positive")
        costs = (self.input_cost_per_million, self.output_cost_per_million)
        if (costs[0] is None) != (costs[1] is None):
            raise ValueError("both input and output token prices are required")
        if any(cost is not None and cost < 0 for cost in costs):
            raise ValueError("token prices cannot be negative")


@dataclass(frozen=True, slots=True)
class GeneratedAnswer:
    """Answer output retained by the evaluation report."""

    answer: str
    context: str
    sources: list[dict[str, Any]]


class AnswerGenerator(Protocol):
    async def generate(
        self,
        query: str,
        chunks: list[dict[str, Any]],
        index_version: str,
    ) -> GeneratedAnswer: ...


class QualityJudge(Protocol):
    name: str

    def score(self, results: list[dict[str, Any]]) -> dict[str, dict[str, float]]: ...


class CoreAnswerGenerator:
    """Use the same context and answer implementation as HTTP requests."""

    def __init__(self) -> None:
        self._context_builder = ContextBuilderNode()

    async def generate(
        self,
        query: str,
        chunks: list[dict[str, Any]],
        index_version: str,
    ) -> GeneratedAnswer:
        prepared = self._context_builder.exec((chunks, ""))
        answer = await answer_service.generate(
            AnswerInput(
                query=query,
                context=prepared["context"],
                history=list(prepared["history"]),
                session_id="",
                valid_citation_refs=frozenset(prepared["valid_citation_refs"]),
                index_version=index_version,
            )
        )
        return GeneratedAnswer(
            answer=answer,
            context=prepared["context"],
            sources=list(prepared["sources"]),
        )


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_output(project_root: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=project_root,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except (OSError, subprocess.CalledProcessError):
        return "unavailable"
    return result.stdout.strip()


def _package_versions() -> dict[str, str]:
    packages = (
        "chromadb",
        "fastapi",
        "openai",
        "pocketflow",
        "ragas",
        "sentence-transformers",
    )
    versions = {}
    for package in packages:
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = "not-installed"
    return versions


def _prompt_metadata(project_root: Path) -> dict[str, Any]:
    prompt_root = project_root / "prompts" / settings.prompt_version
    files = []
    for path in sorted(prompt_root.glob("*.yaml")):
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        files.append(
            {
                "path": path.relative_to(project_root).as_posix(),
                "sha256": _sha256_file(path),
                "declared_version": str(payload.get("version", "unknown")),
                "model": str(payload.get("model", settings.llm_model)),
            }
        )
    return {"selected_version": settings.prompt_version, "files": files}


def _index_metadata() -> dict[str, Any]:
    try:
        active = index_catalog.capture()
    except Exception:
        return {"version_id": "unavailable", "manifest": None}
    return {
        "version_id": active.version_id,
        "collection_name": active.collection_name,
        "manifest": active.manifest.to_dict() if active.manifest else None,
    }


def capture_reproducibility(
    catalog: DatasetCatalog,
    config: EvaluationConfig,
    project_root: Path,
) -> dict[str, Any]:
    """Capture every material input without serializing secrets."""
    lock_path = project_root / "uv.lock"
    pyproject_path = project_root / "pyproject.toml"
    status = _git_output(project_root, "status", "--porcelain", "--untracked-files=no")
    diff = _git_output(project_root, "diff", "--binary", "HEAD")
    datasets = [
        {
            "path": dataset.path.relative_to(project_root).as_posix(),
            "dataset_version": dataset.version,
            "sample_count": len(dataset.records),
        }
        for dataset in catalog.datasets
        if any(record["split"] == config.split for record in dataset.records)
        or (not dataset.records and config.split == "final")
    ]
    return {
        "code": {
            "git_commit": _git_output(project_root, "rev-parse", "HEAD"),
            "git_branch": _git_output(project_root, "branch", "--show-current"),
            "dirty": bool(status and status != "unavailable"),
            "tracked_diff_sha256": (
                hashlib.sha256(diff.encode()).hexdigest()
                if diff != "unavailable"
                else "unavailable"
            ),
        },
        "data": {
            "catalog_path": catalog.path.relative_to(project_root).as_posix(),
            "catalog_version": catalog.version,
            "split": config.split,
            "datasets": datasets,
        },
        "prompts": _prompt_metadata(project_root),
        "models": {
            "generation": settings.llm_model,
            "embedding": settings.local_embedding_model,
            "reranker": settings.rerank_model,
        },
        "index_at_start": _index_metadata(),
        "configuration": {
            **asdict(config),
            "rrf_k": settings.rrf_k,
            "vector_top_k": settings.vector_top_k,
            "bm25_top_k": settings.bm25_top_k,
            "rerank_top_k": settings.rerank_top_k,
            "rerank_timeout_seconds": settings.rerank_timeout_seconds,
            "max_context_tokens": settings.max_context_tokens,
        },
        "environment": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "packages": _package_versions(),
        },
        "dependencies": {
            "uv_lock_sha256": _sha256_file(lock_path),
            "uv_lock_format": next(
                (
                    line.split("=", 1)[1].strip()
                    for line in lock_path.read_text(encoding="utf-8").splitlines()
                    if line.startswith("version =")
                ),
                "unknown",
            ),
            "pyproject_sha256": _sha256_file(pyproject_path),
        },
    }


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _usage_from_spans(spans: list[dict[str, Any]]) -> dict[str, Any]:
    model_spans = [
        span["attributes"]
        for span in spans
        if "model" in span.get("attributes", {})
    ]
    prompt_tokens = sum(int(span.get("prompt_tokens", 0)) for span in model_spans)
    completion_tokens = sum(
        int(span.get("completion_tokens", 0)) for span in model_spans
    )
    reported = bool(model_spans) and all(
        bool(span.get("usage_reported")) or bool(span.get("cache_hit"))
        for span in model_spans
    )
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "reported": reported,
        "model_calls": len(model_spans),
        "cache_hits": sum(bool(span.get("cache_hit")) for span in model_spans),
    }


def _cost(usage: dict[str, Any], config: EvaluationConfig) -> float | None:
    if (
        not usage["reported"]
        or config.input_cost_per_million is None
        or config.output_cost_per_million is None
    ):
        return None
    return (
        usage["prompt_tokens"] * config.input_cost_per_million
        + usage["completion_tokens"] * config.output_cost_per_million
    ) / 1_000_000


class EvaluationRunner:
    """Run catalog samples through unified retrieval and generation interfaces."""

    def __init__(
        self,
        *,
        knowledge_system: KnowledgeSystem | None = None,
        answer_generator: AnswerGenerator | None = None,
        judge: QualityJudge | None = None,
        project_root: Path | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._knowledge = knowledge_system or KnowledgeSystem()
        self._answers = answer_generator or CoreAnswerGenerator()
        self._judge = judge
        self._project_root = (
            project_root or Path(__file__).resolve().parents[2]
        ).resolve()
        self._clock = clock

    async def run(
        self,
        catalog: DatasetCatalog,
        config: EvaluationConfig,
    ) -> dict[str, Any]:
        records = list(catalog.records_for(config.split))
        if not records:
            raise ValueError(
                f"evaluation split {config.split!r} has no samples; "
                "complete human review before running it"
            )
        rng = random.Random(config.seed)
        if config.sample_limit is not None and config.sample_limit < len(records):
            rng.shuffle(records)
            records = records[: config.sample_limit]

        run_id = f"eval-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
        started_at = datetime.now(UTC).isoformat()
        report = {
            "schema_version": 1,
            "run_id": run_id,
            "started_at": started_at,
            "finished_at": None,
            "judge": {
                "name": self._judge.name if self._judge else None,
                "status": "enabled" if self._judge else "not_run",
            },
            "reproducibility": capture_reproducibility(
                catalog,
                config,
                self._project_root,
            ),
            "metric_definitions": {
                "retrieval": "Unique expected source documents recovered at rank K.",
                "citations": "Inline references that point to and cover expected source documents.",
                "task_success": (
                    "No execution error; full expected-source recall; non-abstaining answer "
                    "with citations and token F1 >= 0.2, or correct abstention. Judge scores "
                    "must each be >= 0.5 when a judge is enabled."
                ),
                "cost": "USD only when token usage and both caller-supplied prices are available.",
            },
            "modes": {},
        }

        for mode in config.modes:
            mode_results = []
            with scoped_cache_identity(f"{run_id}:{mode}"):
                for sample in records:
                    mode_results.append(
                        await self._run_sample(sample, mode, config)
                    )
            if self._judge is not None:
                judge_scores = await asyncio.to_thread(
                    self._judge.score,
                    mode_results,
                )
                for result in mode_results:
                    result["metrics"] = score_sample(
                        result,
                        result["answer"],
                        result["retrieved_chunks"],
                        result["sources"],
                        k_values=config.metric_k,
                        error_code=result["error_code"],
                        judge_scores=judge_scores.get(result["sample_id"]),
                    )
            report["modes"][mode] = {
                "summary": summarize_results(
                    mode_results,
                    k_values=config.metric_k,
                ),
                "samples": mode_results,
            }

        report["finished_at"] = datetime.now(UTC).isoformat()
        return report

    async def _run_sample(
        self,
        sample: dict[str, Any],
        mode: RetrievalMode,
        config: EvaluationConfig,
    ) -> dict[str, Any]:
        trace = tracer.start_trace(
            request_id=f"eval-{sample['id']}",
            operation="rag_evaluation",
        )
        token = tracer.bind(trace)
        started = self._clock()
        answer = ""
        context = ""
        sources: list[dict[str, Any]] = []
        chunks: list[dict[str, Any]] = []
        query_variants: list[str] = []
        warnings: list[str] = []
        index_version = "unavailable"
        error_code = ""
        error_type = ""
        try:
            retrieval = await self._knowledge.retrieve(
                sample["question"],
                top_k=config.top_k,
                mode=mode,
            )
            chunks = list(retrieval.chunks)
            query_variants = list(retrieval.query_variants)
            warnings = list(retrieval.warnings)
            index_version = retrieval.index_version
            generated = await self._answers.generate(
                sample["question"],
                chunks,
                index_version,
            )
            answer = generated.answer
            context = generated.context
            sources = generated.sources
        except Exception as exc:
            error_code = "evaluation_sample_failed"
            error_type = type(exc).__name__
            tracer.record_error(error_code, trace)
        finally:
            latency_ms = max(0.0, (self._clock() - started) * 1000)
            tracer.reset(token)

        serialized_chunks = _json_safe(chunks)
        serialized_sources = _json_safe(sources)
        usage = _usage_from_spans(trace["spans"])
        result = {
            "sample_id": sample["id"],
            "question": sample["question"],
            "ground_truth": sample.get("ground_truth"),
            "expected_behavior": sample["expected_behavior"],
            "task_types": list(sample["task_types"]),
            "source_documents": _json_safe(sample["source_documents"]),
            "mode": mode,
            "answer": answer,
            "context": context,
            "sources": serialized_sources,
            "retrieved_chunks": serialized_chunks,
            "query_variants": query_variants,
            "warnings": warnings,
            "index_version": index_version,
            "latency_ms": round(latency_ms, 2),
            "usage": usage,
            "cost_usd": _cost(usage, config),
            "error_code": error_code,
            "error_type": error_type,
            "trace_spans": _json_safe(trace["spans"]),
        }
        result["metrics"] = score_sample(
            result,
            answer,
            serialized_chunks,
            serialized_sources,
            k_values=config.metric_k,
            error_code=error_code,
        )
        return result


def write_report(path: str | Path, report: dict[str, Any]) -> Path:
    """Atomically persist a complete report."""
    destination = Path(path).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination
