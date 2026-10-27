"""Request-local account authority shared by HTTP, workers and commit seams."""

import threading
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from src.security.session_ids import account_scope, account_scope_from_session


class AuthorizationRevoked(RuntimeError):
    """No credentials or resource contents in exceptions crossing execution layers."""


class PrivateSessionError(RuntimeError):
    """Unknown and invisible resources deliberately share one error."""


class ConversationBusy(RuntimeError):
    """One account conversation already has an active generation."""


class ConversationConflict(RuntimeError):
    """Owner-visible mode or capacity conflict without exposing other owners."""


class AccountLimitExceeded(RuntimeError):
    """Stable model quota rejection, never a provider or transport failure."""


@dataclass(frozen=True)
class RequestAuthorization:
    user_id: str
    check_session: Callable[[], bool] = field(repr=False)
    guard_session: Callable[[], AbstractContextManager[bool]] = field(repr=False)
    revoked: threading.Event = field(default_factory=threading.Event, repr=False)
    generation: bool = False
    cancel_task: Callable[[], None] = field(default=lambda: None, repr=False)
    sessions: set[str] = field(default_factory=set, repr=False)
    terminal: threading.Event = field(default_factory=threading.Event, repr=False)
    role: str = "member"
    source_scope: str = "local"

    def active(self) -> bool:
        if self.revoked.is_set():
            return False
        if not self.check_session():
            self.revoked.set()
            return False
        return True


_CURRENT: ContextVar[RequestAuthorization | None] = ContextVar(
    "request_authorization", default=None
)
_RUNS: dict[str, RequestAuthorization] = {}
_RUNS_LOCK: threading.RLock = threading.RLock()


def register_generation(session_id: str) -> None:
    authorization: RequestAuthorization | None = _CURRENT.get()
    if authorization is None or not authorization.generation:
        return
    with _RUNS_LOCK:
        previous: RequestAuthorization | None = _RUNS.get(session_id)
        if (
            previous is not None
            and previous is not authorization
            and not previous.revoked.is_set()
        ):
            raise ConversationBusy("conversation_busy")
        _RUNS[session_id] = authorization
        authorization.sessions.add(session_id)


def cancel_session_generation(session_id: str) -> None:
    require_session_owner(session_id)
    with _RUNS_LOCK:
        authorization: RequestAuthorization | None = _RUNS.get(session_id)
        if authorization is not None:
            authorization.revoked.set()
            authorization.cancel_task()


def mark_stream_terminal() -> None:
    """Mark a terminal accepted by send, never a frame only prepared for yield."""
    authorization: RequestAuthorization | None = _CURRENT.get()
    if authorization is not None:
        authorization.terminal.set()


def current_authorization() -> RequestAuthorization | None:
    return _CURRENT.get()


def require_active_authorization() -> None:
    authorization: RequestAuthorization | None = _CURRENT.get()
    if authorization is not None and not authorization.active():
        raise AuthorizationRevoked("auth_required")


def require_session_owner(session_id: str) -> None:
    authorization: RequestAuthorization | None = _CURRENT.get()
    if authorization is not None and account_scope_from_session(
        session_id
    ) != account_scope(authorization.user_id):
        raise PrivateSessionError("private_session_not_found")


@contextmanager
def bind_authorization(authorization: RequestAuthorization) -> Iterator[None]:
    token = _CURRENT.set(authorization)
    try:
        yield
    finally:
        with _RUNS_LOCK:
            for session_id in authorization.sessions:
                if _RUNS.get(session_id) is authorization:
                    del _RUNS[session_id]
        _CURRENT.reset(token)


@contextmanager
def authorization_commit_guard() -> Iterator[bool]:
    """Hold the auth DB read transaction only across one non-blocking COMMIT.

    Credential mutations cannot acknowledge revocation before this transaction
    finishes. Release between busy retries, so logout is never held behind a
    history-reader lock. No cross-file pseudo-transaction or process-global lock.
    """
    authorization: RequestAuthorization | None = _CURRENT.get()
    if authorization is None:
        yield True
    elif authorization.revoked.is_set():
        yield False
    else:
        with authorization.guard_session() as valid:
            yield valid and not authorization.revoked.is_set()
