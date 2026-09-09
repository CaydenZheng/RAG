"""Validation and identity scoping for public session identifiers."""

import hashlib
import re
from typing import Literal

SessionNamespace = Literal["rag", "agent"]

PUBLIC_SESSION_ID_MAX_LENGTH = 64
CLIENT_ID_PATTERN = re.compile(r"[0-9a-f]{32}")
PUBLIC_SESSION_ID_PATTERN = re.compile(
    rf"[A-Za-z0-9][A-Za-z0-9_-]{{0,{PUBLIC_SESSION_ID_MAX_LENGTH - 1}}}"
)
SCOPED_SESSION_ID_PATTERN = re.compile(
    r"(?P<client_scope>[0-9a-f]{32})_[0-9a-f]{32}"
)


def validate_client_id(client_id: str) -> str:
    """Return a valid opaque client identity or raise ValueError."""
    if not isinstance(client_id, str) or CLIENT_ID_PATTERN.fullmatch(client_id) is None:
        raise ValueError("invalid client identity")
    return client_id


def validate_public_session_id(session_id: str, *, allow_empty: bool = False) -> str:
    """Validate an API-facing session ID with a path-safe ASCII allowlist."""
    if allow_empty and session_id == "":
        return session_id
    if (
        not isinstance(session_id, str)
        or PUBLIC_SESSION_ID_PATTERN.fullmatch(session_id) is None
    ):
        raise ValueError(
            "invalid session ID: use 1-64 ASCII letters, digits, underscores, or hyphens"
        )
    return session_id


def scoped_session_id(
    client_id: str, session_id: str, namespace: SessionNamespace
) -> str:
    """Bind a public session ID to one client without storing either credential."""
    client_id = validate_client_id(client_id)
    session_id = validate_public_session_id(session_id)
    if namespace not in {"rag", "agent"}:
        raise ValueError("invalid session namespace")
    client_scope = hashlib.sha256(client_id.encode()).hexdigest()[:32]
    session_scope = hashlib.sha256(
        f"{namespace}\0{session_id}".encode()
    ).hexdigest()[:32]
    return f"{client_scope}_{session_scope}"


def validate_storage_session_id(session_id: str) -> str:
    """Accept legacy internal IDs and server-generated scoped IDs at storage seams."""
    if (
        isinstance(session_id, str)
        and SCOPED_SESSION_ID_PATTERN.fullmatch(session_id) is not None
    ):
        return session_id
    return validate_public_session_id(session_id)


def client_scope_from_scoped_session(session_id: str) -> str | None:
    """Return the one-way client scope of a storage key, when present."""
    match = SCOPED_SESSION_ID_PATTERN.fullmatch(session_id)
    return match.group("client_scope") if match else None
