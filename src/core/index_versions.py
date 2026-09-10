"""Versioned index manifests and the atomic active-index pointer."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

LEGACY_INDEX_VERSION = "legacy"
LEGACY_COLLECTION_NAME = "rag_collection"
_VERSION_ID_PATTERN = re.compile(r"[0-9a-f]{24}")
_CHECKSUM_PATTERN = re.compile(r"[0-9a-f]{64}")


def validate_index_version_id(value: str) -> str:
    """Return one content-addressed version ID or raise."""
    if _VERSION_ID_PATTERN.fullmatch(value) is None:
        raise ValueError("invalid index version ID")
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SourceVersion:
    """Trace one logical source to the content used by this index."""

    document_id: str
    source: str
    checksum: str

    def __post_init__(self) -> None:
        if not self.document_id:
            raise ValueError("source document ID cannot be empty")
        if _CHECKSUM_PATTERN.fullmatch(self.checksum) is None:
            raise ValueError("invalid source checksum")


@dataclass(frozen=True)
class BuildManifest:
    """Parser, chunker, and embedding inputs that affect index semantics."""

    parser: str
    chunker: str
    chunk_size: int
    chunk_overlap: int
    embedding_model: str
    embedding_dimension: int

    def __post_init__(self) -> None:
        if not self.parser or not self.chunker or not self.embedding_model:
            raise ValueError("index build manifest names cannot be empty")
        if self.chunk_size < 1 or not 0 <= self.chunk_overlap < self.chunk_size:
            raise ValueError("invalid index chunking parameters")
        if self.embedding_dimension < 1:
            raise ValueError("invalid embedding dimension")


@dataclass(frozen=True)
class IndexVersion:
    """Immutable identity and provenance for one complete index candidate."""

    version_id: str
    content_checksum: str
    collection_name: str
    chunk_count: int
    sources: tuple[SourceVersion, ...]
    build: BuildManifest
    created_at: str

    def __post_init__(self) -> None:
        validate_index_version_id(self.version_id)
        if _CHECKSUM_PATTERN.fullmatch(self.content_checksum) is None:
            raise ValueError("invalid index content checksum")
        if self.collection_name != f"rag_v_{self.version_id}":
            raise ValueError("index collection does not match its version")
        if self.chunk_count < 0:
            raise ValueError("index chunk count cannot be negative")
        if self.chunk_count == 0 and self.sources:
            raise ValueError("empty index cannot contain sources")
        if self.chunk_count > 0 and not self.sources:
            raise ValueError("nonempty index must contain a source")

    @classmethod
    def create(
        cls,
        chunks: Iterable[Mapping[str, Any]],
        *,
        parser: str,
        chunker: str,
        chunk_size: int,
        chunk_overlap: int,
        embedding_model: str,
        embedding_dimension: int | None = None,
    ) -> "IndexVersion":
        records = [dict(chunk) for chunk in chunks]

        chunk_ids = [str(chunk.get("chunk_id", "")) for chunk in records]
        if any(not chunk_id for chunk_id in chunk_ids):
            raise ValueError("every indexed chunk must have a chunk_id")
        if len(set(chunk_ids)) != len(chunk_ids):
            raise ValueError("indexed chunk IDs must be unique")

        dimensions = {
            len(chunk.get("embedding") or [])
            for chunk in records
        }
        if records:
            if len(dimensions) != 1 or 0 in dimensions:
                raise ValueError(
                    "all indexed embeddings must have one non-zero dimension"
                )
            resolved_embedding_dimension = dimensions.pop()
        else:
            if embedding_dimension is None or embedding_dimension < 1:
                raise ValueError(
                    "empty index requires the embedding dimension"
                )
            resolved_embedding_dimension = embedding_dimension

        source_records: dict[str, list[dict[str, Any]]] = {}
        canonical_chunks: list[dict[str, Any]] = []
        for chunk in records:
            document_id = str(chunk.get("doc_id", ""))
            if not document_id:
                raise ValueError("every indexed chunk must have a doc_id")
            metadata = dict(chunk.get("metadata") or {})
            metadata.pop("index_version", None)
            metadata.pop("source_version", None)
            record = {
                "chunk_id": str(chunk["chunk_id"]),
                "document_id": document_id,
                "text": str(chunk.get("text", "")),
                "chunk_index": chunk.get(
                    "chunk_index", metadata.get("chunk_index")
                ),
                "metadata": metadata,
            }
            canonical_chunks.append(record)
            source_records.setdefault(document_id, []).append(record)

        canonical_chunks.sort(key=lambda item: item["chunk_id"])
        content_checksum = _sha256(canonical_chunks)
        sources = tuple(
            SourceVersion(
                document_id=document_id,
                source=str(
                    sorted(
                        source_records[document_id],
                        key=lambda item: item["chunk_id"],
                    )[0]["metadata"].get("source", "")
                ),
                checksum=_sha256(
                    sorted(
                        source_records[document_id],
                        key=lambda item: item["chunk_id"],
                    )
                ),
            )
            for document_id in sorted(source_records)
        )
        build = BuildManifest(
            parser=parser,
            chunker=chunker,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            embedding_model=embedding_model,
            embedding_dimension=resolved_embedding_dimension,
        )
        version_id = _sha256(
            {
                "content_checksum": content_checksum,
                "sources": [asdict(source) for source in sources],
                "build": asdict(build),
            }
        )[:24]
        return cls(
            version_id=version_id,
            content_checksum=content_checksum,
            collection_name=f"rag_v_{version_id}",
            chunk_count=len(records),
            sources=sources,
            build=build,
            created_at=datetime.now(timezone.utc).isoformat(),
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "IndexVersion":
        version_id = str(data["version_id"])
        collection_name = str(data["collection_name"])
        return cls(
            version_id=version_id,
            content_checksum=str(data["content_checksum"]),
            collection_name=collection_name,
            chunk_count=int(data["chunk_count"]),
            sources=tuple(
                SourceVersion(**source) for source in data.get("sources", [])
            ),
            build=BuildManifest(**data["build"]),
            created_at=str(data["created_at"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def source_checksum(self, document_id: str) -> str:
        for source in self.sources:
            if source.document_id == document_id:
                return source.checksum
        raise KeyError(document_id)


@dataclass(frozen=True)
class CandidateBatch(Sequence[dict]):
    """Candidate mappings plus the index version captured for their request."""

    index_version: str
    candidates: tuple[dict, ...]

    def __getitem__(self, index):
        return self.candidates[index]

    def __len__(self) -> int:
        return len(self.candidates)


@dataclass(frozen=True)
class ActiveIndex:
    """One request's immutable view of the active index."""

    version_id: str
    collection_name: str
    manifest: IndexVersion | None = None
