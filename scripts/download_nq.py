#!/usr/bin/env python
"""Download Natural Questions records as versioned review candidates."""

from __future__ import annotations

import argparse
import hashlib
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from datasets import load_dataset
from loguru import logger

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.evaluation.datasets import write_dataset  # noqa: E402

DEFAULT_OUT_FILE = PROJECT_ROOT / "data/testset/nq_candidates.json"
DATASET_NAME = "google/natural_questions"
DATASET_SPLIT = "validation"


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _long_answer_text(item: dict[str, Any], start: int, end: int) -> str:
    tokens = item["document"]["tokens"]
    values = tokens["token"]
    is_html = tokens.get("is_html", [False] * len(values))
    return " ".join(
        token
        for token, html in zip(values[start:end], is_html[start:end], strict=True)
        if not html
    ).strip()


def main(
    num_questions: int = 50,
    *,
    output: Path = DEFAULT_OUT_FILE,
) -> int:
    logger.info("Downloading {} {} (streaming)...", DATASET_NAME, DATASET_SPLIT)
    records = load_dataset(DATASET_NAME, split=DATASET_SPLIT, streaming=True)
    generated_at = datetime.now(UTC).isoformat()
    candidates: list[dict[str, Any]] = []

    for record_index, item in enumerate(records):
        long_answers = item["annotations"]["long_answer"]
        if not long_answers:
            continue
        start = long_answers[0]["start_token"]
        end = long_answers[0]["end_token"]
        if start < 0 or end <= start:
            continue

        question = item["question"]["text"].strip()
        short_answers = item["annotations"]["short_answers"]
        texts = short_answers[0]["text"] if short_answers else []
        answers = [answer.strip() for answer in texts if answer.strip()]
        if not question or not answers:
            # Missing annotations are skipped; a question is never its own answer.
            continue

        evidence = _long_answer_text(item, start, end)
        if not evidence:
            continue
        identity = f"{DATASET_SPLIT}:{record_index}:{question}".encode()
        source_uri = f"hf://datasets/{DATASET_NAME}/{DATASET_SPLIT}/{record_index}"
        candidates.append(
            {
                "schema_version": 1,
                "id": f"nq-{_sha256(identity)[:16]}",
                "question": question,
                "ground_truth": "; ".join(dict.fromkeys(answers)),
                "expected_behavior": "answer",
                "task_types": ["factual"],
                "split": "development",
                "source_documents": [
                    {
                        "uri": source_uri,
                        "sha256": _sha256(evidence.encode()),
                        "association": "dataset_annotation",
                    }
                ],
                "evidence_quotes": [evidence],
                "provenance": {
                    "origin": "external_dataset",
                    "generator": "scripts/download_nq.py",
                    "model": "not_applicable",
                    "prompt_version": "not_applicable",
                    "generated_at": generated_at,
                    "source_match_method": "dataset_annotation",
                    "source_dataset": {
                        "name": DATASET_NAME,
                        "configuration": "default",
                        "split": DATASET_SPLIT,
                        "revision": "unknown",
                        "record_index": record_index,
                    },
                },
                "review": {
                    "status": "unverified",
                    "reviewer": None,
                    "reviewed_at": None,
                    "notes": "Imported annotation; project-level human review is still required.",
                },
            }
        )
        if len(candidates) >= num_questions:
            break

    dataset = write_dataset(output, candidates)
    logger.info(
        "Saved {} unverified Natural Questions candidates ({}) to {}",
        len(dataset.records),
        dataset.version,
        output,
    )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--num", type=int, default=50)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUT_FILE)
    arguments = parser.parse_args()
    raise SystemExit(main(arguments.num, output=arguments.output))
