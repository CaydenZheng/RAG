#!/usr/bin/env python
"""Generate versioned, review-only RAG evaluation candidates.

The output is never a final benchmark. Every record keeps the sampled source,
model, prompt version, generation time, and seed needed for later review.
"""

from __future__ import annotations

import argparse
import hashlib
import random
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import settings  # noqa: E402
from src.evaluation import write_dataset  # noqa: E402
from src.llm import llm_client  # noqa: E402

DEFAULT_OUT_FILE = PROJECT_ROOT / "data/testset/generated_candidates.json"
WIKI_DIR = PROJECT_ROOT / "data/raw/wiki"
PROMPT_VERSION = "generate_testset_v2"
WIKIPEDIA_SOURCE = {
    "name": "wikimedia/wikipedia",
    "configuration": "20231101.en",
    "split": "train",
    "revision": "unknown",
}


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sample_id(question: str, source_path: Path) -> str:
    identity = f"{source_path.as_posix()}\n{question}".encode()
    return f"ai-v2-{_sha256(identity)[:16]}"


def _candidate(
    *,
    question: str,
    answer: str,
    source_path: Path,
    source_hash: str,
    evidence: list[str],
    generated_at: str,
    seed: int,
    pair_index: int,
) -> dict[str, Any]:
    relative_source = source_path.relative_to(PROJECT_ROOT).as_posix()
    task_types = ["factual"]
    if pair_index == 1:
        task_types.append("multi_hop")
    return {
        "schema_version": 1,
        "id": _sample_id(question, source_path),
        "question": question,
        "ground_truth": answer,
        "expected_behavior": "answer",
        "task_types": task_types,
        "split": "development",
        "source_documents": [
            {
                "path": relative_source,
                "sha256": source_hash,
                "association": "explicit",
            }
        ],
        "evidence_quotes": evidence,
        "provenance": {
            "origin": "ai_generated",
            "generator": "scripts/generate_testset.py",
            "model": settings.llm_model,
            "prompt_version": PROMPT_VERSION,
            "generated_at": generated_at,
            "source_match_method": "generation_context",
            "seed": seed,
            "source_dataset": WIKIPEDIA_SOURCE,
        },
        "review": {
            "status": "unverified",
            "reviewer": None,
            "reviewed_at": None,
            "notes": "AI-generated candidate; a human must verify the question, answer, evidence, and task types.",
        },
    }


def main(
    num_questions: int = 50,
    *,
    seed: int = 20260910,
    output: Path = DEFAULT_OUT_FILE,
) -> int:
    articles = sorted(WIKI_DIR.glob("*.txt"))
    if not articles:
        logger.error("No Wikipedia articles found in {}. Run download_wiki.py first.", WIKI_DIR)
        return 1

    rng = random.Random(seed)
    rng.shuffle(articles)
    generated_at = datetime.now(UTC).isoformat()
    candidates: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    logger.info(
        "Found {} articles; generating {} unverified candidates with seed {}",
        len(articles),
        num_questions,
        seed,
    )
    for article_path in articles:
        if len(candidates) >= num_questions:
            break
        source_text = article_path.read_text(encoding="utf-8")
        if len(source_text) < 500:
            continue
        paragraphs = [
            paragraph.strip()
            for paragraph in source_text.split("\n\n")
            if len(paragraph.strip()) > 100
        ]
        eligible = paragraphs[1:-1]
        if not eligible:
            continue
        sampled = rng.sample(eligible, min(2, len(eligible)))
        evidence = []
        remaining = 1500
        for paragraph in sampled:
            excerpt = paragraph[:remaining]
            if excerpt:
                evidence.append(excerpt)
                remaining -= len(excerpt) + 2
            if remaining <= 0:
                break
        context = "\n\n".join(evidence)
        prompt = f"""You are generating evaluation candidates for a RAG system.

Article: {article_path.stem}
Passage:
---
{context}
---

Generate two question-answer pairs grounded only in the passage.
The second pair should require combining facts when the passage supports it.
Answers must be concise and complete. Return only valid YAML:

qa_pairs:
  - question: "first question"
    answer: "first answer"
  - question: "second question"
    answer: "second answer"
"""
        try:
            response = llm_client.chat(
                [{"role": "user", "content": prompt}],
                skip_cache=True,
            )
            yaml_fence = chr(96) * 3 + "yaml"
            closing_fence = chr(96) * 3
            yaml_text = (
                response.split(yaml_fence, 1)[1].split(closing_fence, 1)[0].strip()
                if yaml_fence in response
                else response.strip()
            )
            parsed = yaml.safe_load(yaml_text)
            pairs = parsed.get("qa_pairs", []) if isinstance(parsed, dict) else []
            source_hash = _sha256(article_path.read_bytes())
            for pair_index, pair in enumerate(pairs[:2]):
                if not isinstance(pair, dict):
                    continue
                question = str(pair.get("question", "")).strip()
                answer = str(pair.get("answer", "")).strip()
                if not question or not answer:
                    continue
                record = _candidate(
                    question=question,
                    answer=answer,
                    source_path=article_path,
                    source_hash=source_hash,
                    evidence=evidence,
                    generated_at=generated_at,
                    seed=seed,
                    pair_index=pair_index,
                )
                if record["id"] in seen_ids:
                    continue
                candidates.append(record)
                seen_ids.add(record["id"])
                logger.info("[{}/{}] {}", len(candidates), num_questions, question[:80])
                if len(candidates) >= num_questions:
                    break
        except Exception as exc:
            logger.warning("Generation failed for {}: {}", article_path.stem, exc)

    dataset = write_dataset(output, candidates[:num_questions])
    logger.info(
        "Saved {} unverified candidates ({}) to {}",
        len(dataset.records),
        dataset.version,
        output,
    )
    logger.warning(
        "These records are development candidates. Do not add them to final_v1.json "
        "until a named human reviewer verifies them."
    )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--num", type=int, default=50)
    parser.add_argument("--seed", type=int, default=20260910)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUT_FILE)
    arguments = parser.parse_args()
    raise SystemExit(main(arguments.num, seed=arguments.seed, output=arguments.output))
