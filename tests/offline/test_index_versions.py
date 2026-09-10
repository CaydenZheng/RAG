"""Tests for content-addressed index version identity."""

from pathlib import Path


def _chunks(text: str) -> list[dict]:
    return [
        {
            "chunk_id": "doc-1_chunk0",
            "doc_id": "doc-1",
            "text": text,
            "chunk_index": 0,
            "metadata": {
                "source": "guide.md",
                "category": "public",
                "chunk_index": 0,
            },
            "embedding": [1.0, 0.0],
        }
    ]


def test_index_version_changes_with_content_and_build_manifest(
    isolated_runtime: Path,
) -> None:
    from src.core.index_versions import IndexVersion

    inputs = {
        "parser": "parser-v1",
        "chunker": "chunker-v1",
        "chunk_size": 512,
        "chunk_overlap": 50,
        "embedding_model": "embedding-v1",
    }
    first = IndexVersion.create(_chunks("alpha"), **inputs)
    repeated = IndexVersion.create(_chunks("alpha"), **inputs)
    changed_content = IndexVersion.create(_chunks("beta"), **inputs)
    changed_model = IndexVersion.create(
        _chunks("alpha"),
        **{**inputs, "embedding_model": "embedding-v2"},
    )

    assert first.version_id == repeated.version_id
    assert first.version_id != changed_content.version_id
    assert first.version_id != changed_model.version_id
    assert first.collection_name == f"rag_v_{first.version_id}"
    assert first.sources[0].source == "guide.md"
    assert len(first.sources[0].checksum) == 64

