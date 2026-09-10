"""Evaluation data must preserve provenance and the human-review boundary."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from src.evaluation.datasets import (
    ALLOWED_TASK_TYPES,
    DatasetValidationError,
    load_dataset,
    load_dataset_catalog,
    write_dataset,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CATALOG_PATH = REPOSITORY_ROOT / "data/testset/manifest.json"


def test_catalog_separates_unverified_candidates_from_final_data() -> None:
    catalog = load_dataset_catalog(CATALOG_PATH, repository_root=REPOSITORY_ROOT)

    development = catalog.records_for("development")
    final = catalog.records_for("final")
    assert len(development) == 53
    assert final == ()
    assert {task for sample in development for task in sample["task_types"]} == set(
        ALLOWED_TASK_TYPES
    )
    assert all(sample["review"]["status"] != "human_verified" for sample in development)


def test_legacy_ai_samples_are_preserved_and_labelled_unverified() -> None:
    dataset = load_dataset(
        REPOSITORY_ROOT / "data/testset/generated_test.json",
        repository_root=REPOSITORY_ROOT,
        expected_split="development",
    )

    assert len(dataset.records) == 50
    assert len({sample["question"] for sample in dataset.records}) == 50
    assert all(sample["provenance"]["origin"] == "ai_generated" for sample in dataset.records)
    assert all(sample["provenance"]["model"] == "unknown" for sample in dataset.records)
    assert all(sample["review"]["status"] == "unverified" for sample in dataset.records)


def test_source_hash_tampering_is_rejected(tmp_path: Path) -> None:
    source = json.loads(
        (REPOSITORY_ROOT / "data/testset/generated_test.json").read_text(encoding="utf-8")
    )
    tampered = [copy.deepcopy(source[0])]
    tampered[0]["source_documents"][0]["sha256"] = "0" * 64
    path = tmp_path / "tampered.json"
    with pytest.raises(DatasetValidationError, match="sha256 does not match"):
        write_dataset(path, tampered)


def test_unverified_sample_cannot_enter_final_split(tmp_path: Path) -> None:
    source = json.loads(
        (REPOSITORY_ROOT / "data/testset/generated_test.json").read_text(encoding="utf-8")
    )
    unverified = [copy.deepcopy(source[0])]
    unverified[0]["split"] = "final"
    path = tmp_path / "final.json"
    with pytest.raises(DatasetValidationError, match="final samples must be human_verified"):
        write_dataset(path, unverified)


def test_unanswerable_sample_requires_abstention_semantics(tmp_path: Path) -> None:
    source = json.loads(
        (REPOSITORY_ROOT / "data/testset/review_queue_v1.json").read_text(encoding="utf-8")
    )
    sample = next(item for item in source if "unanswerable" in item["task_types"])
    invalid = [copy.deepcopy(sample)]
    invalid[0]["expected_behavior"] = "answer"
    invalid[0]["ground_truth"] = "A made-up answer"
    path = tmp_path / "answerable.json"
    with pytest.raises(DatasetValidationError, match="must pair unanswerable"):
        write_dataset(path, invalid)
