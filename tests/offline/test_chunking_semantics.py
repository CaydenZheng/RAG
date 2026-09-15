"""Acceptance tests for the indexer's character-based chunking contract."""

from pathlib import Path


def _document(text: str, extension: str) -> dict:
    return {
        "doc_id": "guide",
        "text": text,
        "metadata": {
            "extension": extension,
            "source": f"guide{extension}",
        },
    }


def test_markdown_chunking_preserves_heading_sections(
    isolated_runtime: Path,
) -> None:
    from src.core.ingestion import ChunkerNode

    chunks = ChunkerNode().exec(
        _document(
            "## Install\nShort instructions.\n\n## Usage\nAnother short section.",
            ".md",
        )
    )

    assert len(chunks) == 2
    assert "## Install" in chunks[0]["text"]
    assert "## Usage" in chunks[1]["text"]
    assert all(chunk["metadata"]["source"] == "guide.md" for chunk in chunks)


def test_long_text_uses_512_characters_with_50_character_overlap(
    isolated_runtime: Path,
) -> None:
    from src.core.ingestion import ChunkerNode

    chunker = ChunkerNode()
    chunks = chunker.exec(_document("知" * 600, ".txt"))
    texts = [chunk["text"] for chunk in chunks]

    assert chunker.CHUNK_SIZE_CHARS == 512
    assert chunker.CHUNK_OVERLAP_CHARS == 50
    assert [len(text) for text in texts] == [512, 138]
    assert texts[0][-50:] == texts[1][:50]
