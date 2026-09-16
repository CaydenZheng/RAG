"""Offline checks for the privacy-preserving embedding cache assessment."""

import json
import sqlite3
from pathlib import Path

import pytest


def test_query_statistics_normalize_repeats_without_exposing_text() -> None:
    from src.evaluation.embedding_cache_assessment import query_statistics

    secret_query = "  Private\u00a0Question  "

    statistics = query_statistics(
        [secret_query, "private question", "Different question"]
    )

    assert statistics == {
        "status": "measured",
        "total_queries": 3,
        "unique_queries": 2,
        "duplicate_queries": 1,
        "repeat_rate": pytest.approx(1 / 3),
    }
    assert secret_query not in json.dumps(statistics)


def test_missing_session_history_is_not_measurable(tmp_path: Path) -> None:
    from src.evaluation.embedding_cache_assessment import session_query_statistics

    statistics = session_query_statistics(tmp_path / "sessions.db")

    assert statistics == {
        "status": "not_measurable",
        "scope": "stored_user_turn_proxy",
        "limitation": "not_a_complete_embedding_workload",
        "reason": "sessions_db_missing",
        "total_queries": 0,
        "unique_queries": 0,
        "duplicate_queries": 0,
        "repeat_rate": None,
    }


def test_session_statistics_include_only_user_turns(tmp_path: Path) -> None:
    from src.evaluation.embedding_cache_assessment import session_query_statistics

    db_path = tmp_path / "sessions.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE sessions (role TEXT NOT NULL, content TEXT NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO sessions (role, content) VALUES (?, ?)",
            [
                ("user", "Repeat Me"),
                ("assistant", "Repeat Me"),
                ("user", " repeat   me "),
            ],
        )

    statistics = session_query_statistics(db_path)

    assert statistics == {
        "status": "measured",
        "scope": "stored_user_turn_proxy",
        "limitation": "not_a_complete_embedding_workload",
        "total_queries": 2,
        "unique_queries": 1,
        "duplicate_queries": 1,
        "repeat_rate": 0.5,
    }
    assert "Repeat Me" not in json.dumps(statistics)


def test_evaluation_statistics_measure_recorded_embedding_inputs(
    tmp_path: Path,
) -> None:
    from src.evaluation.embedding_cache_assessment import (
        evaluation_query_statistics,
    )

    report = {
        "modes": {
            "hybrid": {
                "samples": [
                    {
                        "question": "ignored when variants are recorded",
                        "query_variants": ["Alpha", "Beta"],
                    },
                    {"question": "Fallback"},
                ]
            }
        }
    }
    (tmp_path / "run-1.json").write_text(json.dumps(report), encoding="utf-8")
    (tmp_path / "run-2.json").write_text(json.dumps(report), encoding="utf-8")

    statistics = evaluation_query_statistics(tmp_path)

    assert statistics == {
        "status": "measured",
        "scope": "evaluation_only",
        "report_files": 2,
        "total_queries": 6,
        "unique_queries": 3,
        "duplicate_queries": 3,
        "repeat_rate": 0.5,
    }
    assert "Alpha" not in json.dumps(statistics)


def test_document_statistics_separate_duplicates_from_index_overlap() -> None:
    from src.evaluation.embedding_cache_assessment import document_statistics

    statistics = document_statistics(
        ["same", "same", "new"],
        existing_texts=["same", "old"],
    )

    assert statistics == {
        "status": "measured",
        "current_chunks": {
            "total_chunks": 3,
            "unique_chunks": 2,
            "duplicate_chunks": 1,
            "repeat_rate": pytest.approx(1 / 3),
        },
        "existing_index": {
            "status": "measured",
            "total_chunks": 2,
            "unique_chunks": 2,
            "overlap_chunks": 1,
            "current_overlap_rate": 0.5,
        },
    }
    assert "same" not in json.dumps(statistics)


def test_incomplete_manifests_cannot_claim_safe_vector_reuse() -> None:
    from src.evaluation.embedding_cache_assessment import (
        vector_reuse_eligibility,
    )

    eligibility = vector_reuse_eligibility(
        current_identity={
            "embedding_model": "model-a",
            "normalization": "l2",
            "preprocessing": "embed-single-v1",
            "vector_dimension": 768,
        },
        existing_identity={"hnsw:space": "cosine"},
    )

    assert eligibility["status"] == "not_eligible"
    assert eligibility["missing_current_fields"] == ["embedding_revision"]
    assert eligibility["missing_existing_fields"] == [
        "embedding_model",
        "embedding_revision",
        "normalization",
        "preprocessing",
        "vector_dimension",
    ]
    assert eligibility["required_cache_key_fields"] == [
        "embedding_model",
        "embedding_revision",
        "normalization",
        "preprocessing",
        "text_sha256",
        "vector_dimension",
    ]


def test_embedding_benchmark_uses_shared_percentile_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.evaluation import embedding_cache_assessment

    clock = iter([0.0, 0.01, 1.0, 1.02, 2.0, 2.04])
    monkeypatch.setattr(
        embedding_cache_assessment.time,
        "perf_counter",
        lambda: next(clock),
    )

    calls: list[str] = []

    def fake_embedder(text: str) -> list[float]:
        calls.append(text)
        return [0.1, 0.2, 0.3]

    benchmark = embedding_cache_assessment.benchmark_embedding(
        fake_embedder,
        warm_iterations=2,
    )

    assert benchmark["status"] == "measured"
    assert benchmark["warm_iterations"] == 2
    assert benchmark["vector_dimension"] == 3
    assert benchmark["cold_start_ms"] == 10.0
    assert benchmark["warm_mean_ms"] == 30.0
    assert benchmark["warm_p95_ms"] == 39.0
    assert benchmark["monetary_cost_usd"] is None
    assert benchmark["cost_basis"] == "local_compute_unpriced"
    assert len(calls) == 3
    assert len(set(calls)) == 1
    assert calls[0] not in json.dumps(benchmark)


def test_assessment_defers_without_production_evidence_or_safe_vectors() -> None:
    from src.evaluation.embedding_cache_assessment import build_assessment

    report = build_assessment(
        production_queries={
            "status": "not_measurable",
            "reason": "sessions_db_missing",
            "duplicate_queries": 0,
        },
        evaluation_queries={
            "status": "measured",
            "scope": "evaluation_only",
            "duplicate_queries": 7,
        },
        documents={
            "current_chunks": {"duplicate_chunks": 2},
            "existing_index": {"overlap_chunks": 10},
        },
        vector_reuse={"status": "not_eligible"},
        benchmark={"status": "measured", "warm_mean_ms": 10.0},
    )

    assert report["decision"] == {
        "overall": "defer",
        "query_cache": "defer",
        "document_vector_reuse": "defer",
        "rationale": [
            "production_query_history_not_measurable",
            "vector_provenance_not_eligible",
        ],
    }
    assert report["embedding_economics"]["production_query_duplicates"] is None
    assert report["embedding_economics"]["evaluation_query_duplicates"] == 7
    assert report["embedding_economics"]["safe_legacy_vectors_reusable"] == 0
    assert report["embedding_economics"]["estimated_compute_avoided_ms"] == {
        "production_query_proxy": None,
        "evaluation_workload": 70.0,
        "duplicate_document_chunks": 20.0,
        "safe_legacy_vectors": 0.0,
    }
    assert report["embedding_economics"]["monetary_cost_usd"] is None
    assert report["decision_criteria"]["query_cache"] == {
        "required_observation": "complete_embedding_call_hit_rate",
        "value_threshold": "deployment_specific_not_defined",
    }


def test_cli_emits_json_without_source_text(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from scripts.assess_embedding_cache import main

    raw_directory = tmp_path / "data" / "raw"
    raw_directory.mkdir(parents=True)
    secret_document = "Private source document"
    (raw_directory / "source.txt").write_text(secret_document, encoding="utf-8")

    exit_code = main(
        [
            "--sessions-db",
            str(tmp_path / "missing-sessions.db"),
            "--eval-runs",
            str(tmp_path / "missing-eval-runs"),
            "--chroma-dir",
            str(tmp_path / "missing-chroma"),
        ]
    )

    output = capsys.readouterr().out
    report = json.loads(output)
    assert exit_code == 0
    assert report["decision"]["overall"] == "defer"
    assert report["documents"]["current_chunks"]["total_chunks"] == 1
    assert secret_document not in output


def test_cli_reads_legacy_documents_without_loading_hnsw(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from scripts.assess_embedding_cache import main

    raw_directory = tmp_path / "data" / "raw"
    raw_directory.mkdir(parents=True)
    (raw_directory / "source.txt").write_text("shared text", encoding="utf-8")
    chroma_directory = tmp_path / "chroma"
    chroma_directory.mkdir()
    with sqlite3.connect(chroma_directory / "chroma.sqlite3") as connection:
        connection.executescript(
            """
            CREATE TABLE collections (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                dimension INTEGER
            );
            CREATE TABLE collection_metadata (
                collection_id TEXT,
                key TEXT,
                str_value TEXT,
                int_value INTEGER,
                float_value REAL,
                bool_value INTEGER
            );
            CREATE TABLE segments (
                id TEXT PRIMARY KEY,
                collection TEXT
            );
            CREATE TABLE embeddings (
                id INTEGER PRIMARY KEY,
                segment_id TEXT
            );
            CREATE TABLE embedding_metadata (
                id INTEGER,
                key TEXT,
                string_value TEXT
            );
            INSERT INTO collections VALUES ('collection-1', 'rag_collection', 768);
            INSERT INTO collection_metadata
                VALUES ('collection-1', 'hnsw:space', 'cosine', NULL, NULL, NULL);
            INSERT INTO segments VALUES ('metadata-1', 'collection-1');
            INSERT INTO embeddings VALUES (1, 'metadata-1');
            INSERT INTO embedding_metadata
                VALUES (1, 'chroma:document', 'shared text');
            """
        )

    exit_code = main(
        [
            "--sessions-db",
            str(tmp_path / "missing-sessions.db"),
            "--eval-runs",
            str(tmp_path / "missing-eval-runs"),
            "--chroma-dir",
            str(chroma_directory),
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert report["documents"]["existing_index"]["overlap_chunks"] == 1
    assert report["documents"]["existing_index"]["collection_status"] == "measured"
    assert report["vector_reuse"]["status"] == "not_eligible"


def test_session_proxy_never_auto_enables_query_cache() -> None:
    from src.evaluation.embedding_cache_assessment import build_assessment

    report = build_assessment(
        production_queries={
            "status": "measured",
            "scope": "stored_user_turn_proxy",
            "repeat_rate": 1.0,
            "duplicate_queries": 10,
        },
        evaluation_queries={"status": "not_measurable", "duplicate_queries": 0},
        documents={
            "current_chunks": {"duplicate_chunks": 0},
            "existing_index": {"overlap_chunks": 0},
        },
        vector_reuse={"status": "not_eligible"},
    )

    assert report["decision"]["query_cache"] == "defer"
    assert (
        "session_history_is_incomplete_query_proxy" in report["decision"]["rationale"]
    )
