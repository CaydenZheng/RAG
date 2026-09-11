#!/usr/bin/env python
"""Run reproducible RAG evaluation.

Examples:
    python scripts/run_eval.py --split development --limit 5
    python scripts/run_eval.py --split development --ablation
    python scripts/run_eval.py --split final --with-ragas
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.core.knowledge import RETRIEVAL_MODES  # noqa: E402
from src.evaluation.datasets import load_dataset_catalog  # noqa: E402
from src.evaluation.runner import (  # noqa: E402
    EvaluationConfig,
    EvaluationRunner,
    write_report,
)

DEFAULT_MANIFEST = PROJECT_ROOT / "data/testset/manifest.json"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "data/eval-runs"
ABLATION_MODES = (
    "vector_only",
    "bm25_only",
    "hybrid",
    "hybrid+rerank",
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate the unified RAG core and save a reproducible JSON report."
    )
    parser.add_argument(
        "--split",
        required=True,
        choices=("development", "final"),
        help="Development data is exploratory; final requires human verification.",
    )
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    strategy = parser.add_mutually_exclusive_group()
    strategy.add_argument(
        "--mode",
        action="append",
        choices=sorted(RETRIEVAL_MODES),
        help="Retrieval mode; repeat to compare modes. Defaults to hybrid+rerank.",
    )
    strategy.add_argument(
        "--ablation",
        action="store_true",
        help="Run all four retrieval modes. This is intentionally not a CI check.",
    )
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--seed", type=int, default=20260910)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--with-ragas",
        action="store_true",
        help="Run paid, model-based Faithfulness and Relevancy judges.",
    )
    parser.add_argument("--input-cost-per-million", type=float)
    parser.add_argument("--output-cost-per-million", type=float)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    catalog = load_dataset_catalog(
        arguments.manifest,
        repository_root=PROJECT_ROOT,
    )
    modes = (
        ABLATION_MODES
        if arguments.ablation
        else tuple(arguments.mode or ("hybrid+rerank",))
    )
    judge = None
    if arguments.with_ragas:
        from src.evaluation.judges import RagasJudge

        judge = RagasJudge()

    config = EvaluationConfig(
        split=arguments.split,
        modes=modes,
        top_k=arguments.top_k,
        seed=arguments.seed,
        sample_limit=arguments.limit,
        input_cost_per_million=arguments.input_cost_per_million,
        output_cost_per_million=arguments.output_cost_per_million,
    )
    if arguments.split == "development":
        print(
            "WARNING: development samples are not human verified; "
            "results are exploratory."
        )

    report = asyncio.run(
        EvaluationRunner(judge=judge, project_root=PROJECT_ROOT).run(
            catalog,
            config,
        )
    )
    output = arguments.output or (
        DEFAULT_OUTPUT_DIR
        / f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{arguments.split}.json"
    )
    destination = write_report(output, report)
    summaries = {
        mode: payload["summary"]
        for mode, payload in report["modes"].items()
    }
    print(json.dumps(summaries, ensure_ascii=False, indent=2))
    print(f"report={destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
