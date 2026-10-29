"""Request-local transcript snapshot; never tool arguments or internal planning."""

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID


@dataclass(frozen=True)
class ExchangeContext:
    client_turn_id: str
    question: str
    used_model: str | None = None
    index_version: str | None = None
    sources: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = ()
    requested_model: str | None = None
    skills: tuple[dict[str, str], ...] = ()


exchange_context: ContextVar[ExchangeContext | None] = ContextVar(
    "exchange_context", default=None
)
completed_model: ContextVar[str | None] = ContextVar("completed_model", default=None)


def canonical_uuid(value: str) -> str:
    """Validate identifiers without echoing malformed private input."""
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("Invalid identifier") from None


def safe_sources(sources: list[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    """Store a bounded text-only projection, never arbitrary provider metadata."""
    result: list[dict[str, Any]] = []
    for source in sources[:50]:
        ref: Any = source.get("ref")
        if not isinstance(ref, int) or isinstance(ref, bool) or ref < 1:
            continue
        item: dict[str, Any] = {
            "ref": ref,
            "source_kind": "knowledge",
            "label": "知识库来源 " + str(ref),
        }
        # Free source paths/URLs are deliberately not copied into the snapshot.
        for key in ("document_id", "version"):
            value: Any = source.get(key)
            if isinstance(value, str) and len(value) <= 128:
                item[key] = value
        position: Any = source.get("position")
        page: Any = position.get("page") if isinstance(position, dict) else None
        if isinstance(page, int) and not isinstance(page, bool) and page > 0:
            item["page"] = page
        excerpt: Any = source.get("text")
        if isinstance(excerpt, str):
            item["excerpt"] = excerpt[:360]
        result.append(item)
    return tuple(result)
