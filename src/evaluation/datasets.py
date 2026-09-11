"""Versioned evaluation datasets with provenance and review guarantees.

The public interface deliberately stays small: load a standalone dataset, load
the repository catalog, or stamp/write generated candidates. All validation
rules live here so generators and evaluation runners share the same semantics.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
ALLOWED_TASK_TYPES = frozenset(
    {"factual", "entity", "numeric", "temporal", "multi_hop", "unanswerable", "adversarial"}
)
ALLOWED_SPLITS = frozenset({"development", "final"})
ALLOWED_REVIEW_STATUSES = frozenset({"unverified", "ai_screened", "human_verified", "rejected"})
REQUIRED_PROVENANCE_FIELDS = (
    "origin",
    "generator",
    "model",
    "prompt_version",
    "generated_at",
    "source_match_method",
)
_SAMPLE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{5,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class DatasetValidationError(ValueError):
    """Raised when evaluation data cannot be trusted under the catalog rules."""


@dataclass(frozen=True, slots=True)
class EvaluationDataset:
    """A validated dataset and its content-derived version."""

    path: Path
    records: tuple[dict[str, Any], ...]
    version: str

    @property
    def task_types(self) -> frozenset[str]:
        return frozenset(task for record in self.records for task in record["task_types"])


@dataclass(frozen=True, slots=True)
class DatasetCatalog:
    """A validated set of development and final evaluation datasets."""

    path: Path
    datasets: tuple[EvaluationDataset, ...]
    version: str

    def records_for(self, split: str) -> tuple[dict[str, Any], ...]:
        """Return all records in a declared split."""
        if split not in ALLOWED_SPLITS:
            raise ValueError(f"unknown evaluation split: {split}")
        return tuple(
            record
            for dataset in self.datasets
            for record in dataset.records
            if record["split"] == split
        )


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_text(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _sha256_file(path: Path) -> str:
    canonical: str = _canonical_text(path.read_bytes().decode("utf-8"))
    return _sha256_bytes(canonical.encode("utf-8"))


def compute_dataset_version(records: list[dict[str, Any]] | tuple[dict[str, Any], ...]) -> str:
    """Derive a stable version from normalized sample content and source hashes."""
    normalized = [
        {key: value for key, value in record.items() if key != "dataset_version"}
        for record in records
    ]
    normalized.sort(key=lambda item: item.get("id", ""))
    digest = _sha256_bytes(_canonical_json(normalized).encode("utf-8"))
    return f"sha256:{digest}"


def stamp_dataset_version(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Copy records and attach their shared, content-derived dataset version."""
    stamped = copy.deepcopy(records)
    version = compute_dataset_version(stamped)
    for record in stamped:
        record["dataset_version"] = version
    return stamped


def write_dataset(
    path: str | Path,
    records: list[dict[str, Any]],
    *,
    repository_root: str | Path | None = None,
) -> EvaluationDataset:
    """Stamp and write candidate data; catalog publication remains a review step."""
    output_path = Path(path).resolve()
    stamped = stamp_dataset_version(records)
    root = Path(repository_root).resolve() if repository_root else _repository_root(output_path)
    sample_ids: set[str] = set()
    for index, record in enumerate(stamped):
        _validate_record(record, index=index, repository_root=root)
        if record["id"] in sample_ids:
            raise DatasetValidationError(f"duplicate sample id: {record['id']}")
        sample_ids.add(record["id"])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(stamped, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return load_dataset(output_path, repository_root=root)


def _repository_root(path: Path) -> Path:
    for candidate in (path, *path.parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate.resolve()
    raise DatasetValidationError(f"cannot find repository root above {path}")


def _required_text(mapping: dict[str, Any], field: str, label: str) -> str:
    value = mapping.get(field)
    if not isinstance(value, str) or not value.strip():
        raise DatasetValidationError(f"{label}.{field} must be a non-empty string")
    return value


def _local_source_text(
    source: dict[str, Any], *, repository_root: Path, label: str
) -> str | None:
    digest = _required_text(source, "sha256", label)
    if not _SHA256.fullmatch(digest):
        raise DatasetValidationError(f"{label}.sha256 must be a lowercase SHA-256")

    source_path = source.get("path")
    source_uri = source.get("uri")
    if bool(source_path) == bool(source_uri):
        raise DatasetValidationError(f"{label} must contain exactly one of path or uri")
    if source_uri:
        _required_text(source, "uri", label)
        return None

    relative = Path(_required_text(source, "path", label))
    absolute = (repository_root / relative).resolve()
    try:
        absolute.relative_to(repository_root)
    except ValueError as exc:
        raise DatasetValidationError(f"{label}.path leaves the repository") from exc
    if not absolute.is_file():
        raise DatasetValidationError(f"{label}.path does not exist: {relative.as_posix()}")
    if _sha256_file(absolute) != digest:
        raise DatasetValidationError(f"{label}.sha256 does not match {relative.as_posix()}")
    return _canonical_text(absolute.read_bytes().decode("utf-8"))


def _validate_record(record: Any, *, index: int, repository_root: Path) -> None:
    label = f"record[{index}]"
    if not isinstance(record, dict):
        raise DatasetValidationError(f"{label} must be an object")
    if record.get("schema_version") != SCHEMA_VERSION:
        raise DatasetValidationError(f"{label}.schema_version must be {SCHEMA_VERSION}")

    sample_id = _required_text(record, "id", label)
    if not _SAMPLE_ID.fullmatch(sample_id):
        raise DatasetValidationError(f"{label}.id has an invalid format")
    _required_text(record, "question", label)

    expected_behavior = record.get("expected_behavior")
    ground_truth = record.get("ground_truth")
    if expected_behavior == "answer":
        if not isinstance(ground_truth, str) or not ground_truth.strip():
            raise DatasetValidationError(f"{label}.ground_truth is required for answer samples")
    elif expected_behavior == "abstain":
        if ground_truth is not None:
            raise DatasetValidationError(f"{label}.ground_truth must be null for abstain samples")
    else:
        raise DatasetValidationError(f"{label}.expected_behavior must be answer or abstain")

    task_types = record.get("task_types")
    if not isinstance(task_types, list) or not task_types or len(task_types) != len(set(task_types)):
        raise DatasetValidationError(f"{label}.task_types must be a non-empty unique list")
    unknown_types = set(task_types) - ALLOWED_TASK_TYPES
    if unknown_types:
        raise DatasetValidationError(f"{label}.task_types contains unknown values: {sorted(unknown_types)}")
    if ("unanswerable" in task_types) != (expected_behavior == "abstain"):
        raise DatasetValidationError(
            f"{label} must pair unanswerable with expected_behavior=abstain"
        )

    split = record.get("split")
    if split not in ALLOWED_SPLITS:
        raise DatasetValidationError(f"{label}.split must be one of {sorted(ALLOWED_SPLITS)}")

    provenance = record.get("provenance")
    if not isinstance(provenance, dict):
        raise DatasetValidationError(f"{label}.provenance must be an object")
    for field in REQUIRED_PROVENANCE_FIELDS:
        _required_text(provenance, field, f"{label}.provenance")

    review = record.get("review")
    if not isinstance(review, dict) or review.get("status") not in ALLOWED_REVIEW_STATUSES:
        raise DatasetValidationError(f"{label}.review.status is invalid")
    if review["status"] == "human_verified":
        _required_text(review, "reviewer", f"{label}.review")
        _required_text(review, "reviewed_at", f"{label}.review")
    elif review.get("reviewer") is not None or review.get("reviewed_at") is not None:
        raise DatasetValidationError(
            f"{label}.review cannot claim a reviewer before human verification"
        )
    if split == "final" and review["status"] != "human_verified":
        raise DatasetValidationError(f"{label}: final samples must be human_verified")

    sources = record.get("source_documents")
    if not isinstance(sources, list) or not sources:
        raise DatasetValidationError(f"{label}.source_documents must be non-empty")
    local_texts = []
    for source_index, source in enumerate(sources):
        if not isinstance(source, dict):
            raise DatasetValidationError(f"{label}.source_documents[{source_index}] must be an object")
        source_text = _local_source_text(
            source,
            repository_root=repository_root,
            label=f"{label}.source_documents[{source_index}]",
        )
        if source_text is not None:
            local_texts.append(source_text)

    quotes = record.get("evidence_quotes")
    if not isinstance(quotes, list) or not quotes or not all(
        isinstance(quote, str) and quote.strip() for quote in quotes
    ):
        raise DatasetValidationError(f"{label}.evidence_quotes must contain text")
    normalized_sources = [_canonical_text(text) for text in local_texts]
    if normalized_sources and any(
        not any(_canonical_text(quote) in text for text in normalized_sources)
        for quote in quotes
    ):
        raise DatasetValidationError(f"{label}.evidence_quotes must be exact source text")


def load_dataset(
    path: str | Path,
    *,
    repository_root: str | Path | None = None,
    expected_split: str | None = None,
) -> EvaluationDataset:
    """Load and validate one JSON dataset through the shared schema seam."""
    dataset_path = Path(path).resolve()
    root = Path(repository_root).resolve() if repository_root else _repository_root(dataset_path)
    try:
        records = json.loads(dataset_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DatasetValidationError(f"cannot read dataset {dataset_path}: {exc}") from exc
    if not isinstance(records, list):
        raise DatasetValidationError(f"dataset {dataset_path} must be a JSON array")

    ids: set[str] = set()
    for index, record in enumerate(records):
        _validate_record(record, index=index, repository_root=root)
        sample_id = record["id"]
        if sample_id in ids:
            raise DatasetValidationError(f"duplicate sample id: {sample_id}")
        ids.add(sample_id)
        if expected_split is not None and record["split"] != expected_split:
            raise DatasetValidationError(
                f"record {sample_id} uses split {record['split']}, expected {expected_split}"
            )

    version = compute_dataset_version(records)
    for record in records:
        if record.get("dataset_version") != version:
            raise DatasetValidationError(
                f"record {record['id']} has stale dataset_version; expected {version}"
            )
    return EvaluationDataset(dataset_path, tuple(records), version)


def _directory_version(directory: Path, repository_root: Path) -> tuple[str, int]:
    documents = []
    for document in sorted(directory.glob("*.txt"), key=lambda item: item.as_posix()):
        documents.append(
            {
                "path": document.resolve().relative_to(repository_root).as_posix(),
                "sha256": _sha256_file(document),
            }
        )
    digest = _sha256_bytes(_canonical_json(documents).encode("utf-8"))
    return f"sha256:{digest}", len(documents)


def _catalog_version(manifest: dict[str, Any]) -> str:
    normalized = {key: value for key, value in manifest.items() if key != "catalog_version"}
    digest = _sha256_bytes(_canonical_json(normalized).encode("utf-8"))
    return f"sha256:{digest}"


def load_dataset_catalog(
    path: str | Path, *, repository_root: str | Path | None = None
) -> DatasetCatalog:
    """Validate the manifest, corpus version, dataset files, and split policy."""
    manifest_path = Path(path).resolve()
    root = Path(repository_root).resolve() if repository_root else _repository_root(manifest_path)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DatasetValidationError(f"cannot read catalog {manifest_path}: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != SCHEMA_VERSION:
        raise DatasetValidationError(f"catalog schema_version must be {SCHEMA_VERSION}")
    version = _catalog_version(manifest)
    if manifest.get("catalog_version") != version:
        raise DatasetValidationError(f"catalog_version is stale; expected {version}")

    corpus = manifest.get("corpus")
    if not isinstance(corpus, dict):
        raise DatasetValidationError("catalog.corpus must be an object")
    corpus_path = (root / _required_text(corpus, "local_path", "catalog.corpus")).resolve()
    try:
        corpus_path.relative_to(root)
    except ValueError as exc:
        raise DatasetValidationError("catalog.corpus.local_path leaves the repository") from exc
    actual_corpus_version, actual_document_count = _directory_version(corpus_path, root)
    if corpus.get("version") != actual_corpus_version:
        raise DatasetValidationError("catalog corpus version does not match local documents")
    if corpus.get("document_count") != actual_document_count:
        raise DatasetValidationError("catalog corpus document_count is stale")

    entries = manifest.get("datasets")
    if not isinstance(entries, list) or not entries:
        raise DatasetValidationError("catalog.datasets must be a non-empty list")
    datasets = []
    dataset_names: set[str] = set()
    dataset_paths: set[Path] = set()
    for index, entry in enumerate(entries):
        label = f"catalog.datasets[{index}]"
        if not isinstance(entry, dict):
            raise DatasetValidationError(f"{label} must be an object")
        name = _required_text(entry, "name", label)
        if name in dataset_names:
            raise DatasetValidationError(f"duplicate catalog dataset name: {name}")
        dataset_names.add(name)

        relative_path = Path(_required_text(entry, "path", label))
        dataset_path = (root / relative_path).resolve()
        try:
            dataset_path.relative_to(root)
        except ValueError as exc:
            raise DatasetValidationError(f"{label}.path leaves the repository") from exc
        if dataset_path in dataset_paths:
            raise DatasetValidationError(f"duplicate catalog dataset path: {relative_path.as_posix()}")
        dataset_paths.add(dataset_path)

        split = _required_text(entry, "split", label)
        if split not in ALLOWED_SPLITS:
            raise DatasetValidationError(f"{label}.split must be one of {sorted(ALLOWED_SPLITS)}")
        review_policy = _required_text(entry, "review_status", label)
        if review_policy not in {"unverified", "human_verified_only"}:
            raise DatasetValidationError(f"{label}.review_status is invalid")
        if split == "final" and review_policy != "human_verified_only":
            raise DatasetValidationError(f"{label}: final data requires human_verified_only")

        dataset = load_dataset(
            dataset_path,
            repository_root=root,
            expected_split=split,
        )
        if entry.get("sha256") != _sha256_file(dataset_path):
            raise DatasetValidationError(f"{label}.sha256 does not match {relative_path.as_posix()}")
        if entry.get("dataset_version") != dataset.version:
            raise DatasetValidationError(f"{label}.dataset_version is stale")
        if entry.get("sample_count") != len(dataset.records):
            raise DatasetValidationError(f"{label}.sample_count is stale")
        statuses = {record["review"]["status"] for record in dataset.records}
        expected_status = (
            "human_verified" if review_policy == "human_verified_only" else review_policy
        )
        if statuses - {expected_status}:
            raise DatasetValidationError(
                f"{label}.review_status does not match its records"
            )
        datasets.append(dataset)

    required_types = manifest.get("required_task_types")
    if not isinstance(required_types, list) or set(required_types) != ALLOWED_TASK_TYPES:
        raise DatasetValidationError("catalog.required_task_types must list every supported type")
    covered_types = set().union(*(dataset.task_types for dataset in datasets))
    missing_types = ALLOWED_TASK_TYPES - covered_types
    if missing_types:
        raise DatasetValidationError(f"catalog is missing task coverage: {sorted(missing_types)}")

    return DatasetCatalog(manifest_path, tuple(datasets), version)
