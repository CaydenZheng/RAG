"""Shared read-only knowledge projection from one captured version manifest."""

import re
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from src.core.index_versions import ActiveIndex
from src.core.run_options import WorkspaceError
from src.infra.index_catalog import index_catalog
from src.security.request_authorization import PrivateSessionError

router: APIRouter = APIRouter(prefix="/knowledge/documents", tags=["knowledge"])


class SearchDocuments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title_query: str = Field(min_length=1, max_length=80)
    cursor: str | None = Field(default=None, max_length=128)


def _active() -> ActiveIndex:
    try:
        active = index_catalog.capture()
        if active.manifest is None:
            raise WorkspaceError("knowledge_unavailable", 503)
        return active
    except (OSError, ValueError, KeyError):
        raise WorkspaceError("knowledge_unavailable", 503) from None


def _label(source: str, document_id: str) -> str:
    # A source string is never a local path or automatically visited URL.
    label: str = source.split("?", 1)[0].split("#", 1)[0].replace("\\", "/").rsplit("/", 1)[-1]
    return "".join(char for char in label if ord(char) >= 32)[:255] or "文档 " + document_id[:12]


def _listing(query: str = "", cursor: str | None = None) -> dict[str, Any]:
    active = _active()
    assert active.manifest is not None
    offset: int = 0
    if cursor:
        parts: list[str] = cursor.split(":")
        if len(parts) != 2 or not parts[1].isdigit() or len(parts[1]) > 8:
            raise WorkspaceError("auth_invalid_request")
        if parts[0] != active.version_id:
            raise WorkspaceError("knowledge_changed", 409)
        offset = int(parts[1])
    items: list[dict[str, Any]] = [{"id": source.document_id, "name": _label(source.source, source.document_id),
        "checksum": source.checksum, "index_version": active.version_id, "indexed_at": active.manifest.created_at,
        "updated_at": None, "status": "indexed"} for source in active.manifest.sources]
    if query:
        items = [item for item in items if query.casefold() in item["name"].casefold()]
    return {"items": items[offset:offset + 20], "index_version": active.version_id,
            "next_cursor": f"{active.version_id}:{offset + 20}" if offset + 20 < len(items) else None}


@router.get("")
def listing(cursor: str | None = Query(default=None, max_length=128)) -> dict[str, Any]:
    return _listing(cursor=cursor)


@router.post("/search")
def search(body: SearchDocuments) -> dict[str, Any]:
    return _listing(body.title_query, body.cursor)


@router.get("/{document_id}")
def detail(document_id: str, version: str = Query(max_length=24)) -> dict[str, Any]:
    active = _active()
    if active.version_id != version:
        raise WorkspaceError("knowledge_changed", 409)
    assert active.manifest is not None
    source = next((item for item in active.manifest.sources if item.document_id == document_id), None)
    if source is None or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", document_id):
        raise PrivateSessionError("private_session_not_found")
    try:
        from src.infra.chroma_clients import get_chroma_client

        collection = get_chroma_client().get_collection(active.collection_name)
        # IndexBuilderNode writes source_version to metadata; doc_id is only a
        # top-level ingestion field. Use captured manifest provenance for both
        # existing and newly built versioned collections.
        records = collection.get(where={"source_version": source.checksum}, include=["documents", "metadatas"], limit=21)
        pairs = sorted(zip(records["documents"], records["metadatas"], strict=True), key=lambda pair: pair[1].get("chunk_index", 0))
        # This is an explicit bounded excerpt, not a whole-document promise.
        text: str = "\n\n".join(pair[0] for pair in pairs[:20])[:20_000]
    except Exception:
        raise WorkspaceError("knowledge_unavailable", 503) from None
    return {"id": document_id, "name": _label(source.source, document_id), "index_version": version,
            "checksum": source.checksum, "text": text, "excerpt_only": True}
