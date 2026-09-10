"""Request-local identity used to partition exact LLM cache entries."""

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_identity_scope: ContextVar[str] = ContextVar(
    "llm_cache_identity", default="shared"
)


def current_cache_identity() -> str:
    return _identity_scope.get()


@contextmanager
def scoped_cache_identity(client_id: str) -> Iterator[None]:
    """Use a non-reversible client scope for all LLM calls in one request."""
    scope = hashlib.sha256(client_id.encode("utf-8")).hexdigest()[:32]
    token = _identity_scope.set(scope)
    try:
        yield
    finally:
        _identity_scope.reset(token)


