"""Assess embedding cache value without enabling a cache."""

from __future__ import annotations

import argparse
import gc
import json
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import settings  # noqa: E402
from src.core.ingestion import (  # noqa: E402
    CHUNKER_VERSION,
    PARSER_VERSION,
    ChunkerNode,
    DocDeduplicatorNode,
    DocLoaderNode,
)
from src.evaluation.embedding_cache_assessment import (  # noqa: E402
    benchmark_embedding,
    build_assessment,
    document_statistics,
    evaluation_query_statistics,
    session_query_statistics,
    vector_reuse_eligibility,
)

DEFAULT_SESSIONS_DB = PROJECT_ROOT / "data/sessions.db"
DEFAULT_EVAL_RUNS = PROJECT_ROOT / "data/eval-runs"
DEFAULT_CHROMA_DIR = PROJECT_ROOT / "data/chroma"
DEFAULT_COLLECTION = "rag_collection"


def _load_current_chunk_texts() -> tuple[list[str], int]:
    loader = DocLoaderNode()
    documents = [
        document.to_dict()
        for file_path in loader.prep({})
        if (document := loader.exec(file_path)) is not None
    ]
    unique_documents = DocDeduplicatorNode().exec(documents)
    chunker = ChunkerNode()
    chunks = [
        chunk for document in unique_documents for chunk in chunker.exec(document)
    ]
    return [chunk["text"] for chunk in chunks], len(unique_documents)


def _metadata_value(row: sqlite3.Row) -> object:
    for column in ("str_value", "int_value", "float_value", "bool_value"):
        value = row[column]
        if value is not None:
            return bool(value) if column == "bool_value" else value
    return None


def _read_existing_collection(
    chroma_directory: Path,
    collection_name: str,
) -> tuple[list[str] | None, dict[str, Any], dict[str, object]]:
    database_path = chroma_directory / "chroma.sqlite3"
    if not database_path.is_file():
        return (
            None,
            {},
            {
                "collection_status": "not_measurable",
                "collection_reason": "chroma_database_missing",
                "collection_name": collection_name,
            },
        )

    database_uri = f"file:{database_path.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(database_uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        collection = connection.execute(
            "SELECT id, dimension FROM collections WHERE name = ?",
            (collection_name,),
        ).fetchone()
        if collection is None:
            return (
                None,
                {},
                {
                    "collection_status": "not_measurable",
                    "collection_reason": "collection_missing",
                    "collection_name": collection_name,
                },
            )

        metadata_rows = connection.execute(
            "SELECT key, str_value, int_value, float_value, bool_value "
            "FROM collection_metadata WHERE collection_id = ?",
            (collection["id"],),
        )
        identity = {row["key"]: _metadata_value(row) for row in metadata_rows}
        identity["vector_dimension"] = collection["dimension"]
        document_rows = connection.execute(
            "SELECT em.string_value "
            "FROM embedding_metadata AS em "
            "JOIN embeddings AS e ON e.id = em.id "
            "JOIN segments AS s ON s.id = e.segment_id "
            "WHERE s.collection = ? "
            "AND em.key = 'chroma:document'",
            (collection["id"],),
        )
        documents = [
            row["string_value"]
            for row in document_rows
            if isinstance(row["string_value"], str)
        ]

    return (
        documents,
        identity,
        {
            "collection_status": "measured",
            "collection_name": collection_name,
        },
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure whether embedding caching is justified."
    )
    parser.add_argument("--sessions-db", type=Path, default=DEFAULT_SESSIONS_DB)
    parser.add_argument("--eval-runs", type=Path, default=DEFAULT_EVAL_RUNS)
    parser.add_argument("--chroma-dir", type=Path, default=DEFAULT_CHROMA_DIR)
    parser.add_argument("--collection", default=DEFAULT_COLLECTION)
    parser.add_argument("--benchmark", action="store_true")
    parser.add_argument("--benchmark-iterations", type=int, default=5)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    benchmark: dict[str, object] | None = None
    if args.benchmark:
        from src.llm import llm_client

        benchmark = benchmark_embedding(
            llm_client.embed_single,
            warm_iterations=args.benchmark_iterations,
        )
        llm_client._embedding_model = None
        gc.collect()

    current_texts, source_documents = _load_current_chunk_texts()
    existing_texts, existing_identity, collection_status = _read_existing_collection(
        args.chroma_dir, args.collection
    )
    documents = document_statistics(
        current_texts,
        existing_texts=existing_texts,
    )
    documents["source_documents"] = source_documents
    documents["existing_index"].update(collection_status)
    del current_texts, existing_texts

    current_identity = {
        "embedding_model": settings.local_embedding_model,
        "embedding_revision": None,
        "normalization": None,
        "preprocessing": (
            f"{PARSER_VERSION}/{CHUNKER_VERSION}/"
            f"{ChunkerNode.CHUNK_SIZE}/{ChunkerNode.CHUNK_OVERLAP}"
        ),
        "vector_dimension": (
            benchmark["vector_dimension"] if benchmark is not None else None
        ),
    }
    vector_reuse = vector_reuse_eligibility(
        current_identity=current_identity,
        existing_identity=existing_identity,
    )
    report = build_assessment(
        production_queries=session_query_statistics(args.sessions_db),
        evaluation_queries=evaluation_query_statistics(args.eval_runs),
        documents=documents,
        vector_reuse=vector_reuse,
        benchmark=benchmark,
    )
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output is None:
        print(encoded, end="")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
