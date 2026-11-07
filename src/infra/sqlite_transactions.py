"""Bounded SQLite lock attempts; the business body is never replayed."""

import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager, nullcontext
from dataclasses import dataclass
from typing import NoReturn

from loguru import logger

from src.core.run_options import WorkspaceError
from src.security.request_authorization import (
    AuthorizationRevoked,
    authorization_commit_guard,
    require_active_authorization,
)

RETRY_INTERVAL = .005


class CommitPermit:
    """Order cancellation acknowledgement with one non-blocking commit attempt."""

    def __init__(self, deadline: float | None = None) -> None:
        self._deadline = deadline
        self._revoked = threading.Event()
        self._lock = threading.Lock()

    def __call__(self) -> bool:
        return not self._revoked.is_set() and (self._deadline is None or time.monotonic() < self._deadline)

    def revoke(self) -> None:
        with self._lock:
            self._revoked.set()

    @contextmanager
    def guard(self) -> Iterator[bool]:
        with self._lock:
            yield self()


@dataclass(frozen=True)
class WritePolicy:
    begin_budget: float
    commit_budget: float
    busy_code: str
    unavailable_code: str


def storage_error(error: sqlite3.Error, policy: WritePolicy, phase: str) -> NoReturn:
    from src.infra.tracer import tracer

    code = getattr(error, "sqlite_errorcode", 0)
    logger.warning("SQLite operation failed: phase={}, sqlite_code={}, request_id={}",
                   phase, code, (tracer.current or {}).get("request_id", ""))
    raise WorkspaceError(policy.busy_code if code & 0xFF == sqlite3.SQLITE_BUSY else policy.unavailable_code, 503) from error


@contextmanager
def storage_connection(connect: Callable[[], sqlite3.Connection], policy: WritePolicy) -> Iterator[sqlite3.Connection]:
    """Own connection lifetime and classify failures, including setup and close.

    A factory must close any connection it acquired if setup fails before return.
    """
    phase = "connect"
    try:
        with closing(connect()) as connection:
            phase = "operation"
            yield connection
            phase = "close"
    except sqlite3.Error as error:
        storage_error(error, policy, phase)


def guarded_commit(connection: sqlite3.Connection, permit: Callable[[], bool] | None,
                   *, budget: float, authorize: bool = True) -> bool:
    """Retry only COMMIT of this transaction, releasing guards between attempts."""
    previous_wait = connection.execute("PRAGMA busy_timeout").fetchone()[0]
    deadline = time.monotonic() + budget
    connection.execute("PRAGMA busy_timeout=0")
    try:
        while True:
            try:
                with authorization_commit_guard() if authorize else nullcontext(True) as authorized:
                    if not authorized:
                        connection.rollback()
                        return False
                    with permit.guard() if isinstance(permit, CommitPermit) else nullcontext(permit is None or permit()) as permitted:
                        if not permitted:
                            connection.rollback()
                            return False
                        connection.commit()
                        return True
            except sqlite3.OperationalError as error:
                remaining = deadline - time.monotonic()
                if getattr(error, "sqlite_errorcode", 0) & 0xFF != sqlite3.SQLITE_BUSY or remaining <= 0:
                    raise
                time.sleep(min(RETRY_INTERVAL, remaining))
    finally:
        connection.execute(f"PRAGMA busy_timeout={previous_wait}")


class SQLiteWrite:
    def __init__(self, connection: sqlite3.Connection, policy: WritePolicy,
                 permit: CommitPermit | None, authorize: bool) -> None:
        self.connection = connection
        self.policy = policy
        self.permit = permit
        self.authorize = authorize

    def begin(self) -> None:
        deadline = time.monotonic() + self.policy.begin_budget
        self.connection.execute("PRAGMA busy_timeout=0")
        while True:
            if self.authorize:
                require_active_authorization()
            if self.permit is not None and not self.permit():
                raise AuthorizationRevoked("auth_required")
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                return
            except sqlite3.OperationalError as error:
                remaining = deadline - time.monotonic()
                if getattr(error, "sqlite_errorcode", 0) & 0xFF != sqlite3.SQLITE_BUSY or remaining <= 0:
                    storage_error(error, self.policy, "begin")
                time.sleep(min(RETRY_INTERVAL, remaining))

    def commit(self) -> None:
        try:
            if not guarded_commit(self.connection, self.permit, budget=self.policy.commit_budget, authorize=self.authorize):
                raise AuthorizationRevoked("auth_required")
        except sqlite3.Error as error:
            storage_error(error, self.policy, "commit")


@contextmanager
def write_transaction(connect: Callable[[], sqlite3.Connection], policy: WritePolicy,
                      permit: CommitPermit | None = None, *, authorize: bool = True) -> Iterator[SQLiteWrite]:
    """Enter once; caller commits explicitly. Empty/failed bodies roll back.

    authorize=False is reserved for internal tombstoning/cleanup, never user data
    mutation. File compensation remains the owning store's responsibility.
    """
    with storage_connection(connect, policy) as connection:
        transaction = SQLiteWrite(connection, policy, permit, authorize)
        try:
            transaction.begin()
            yield transaction
        except sqlite3.Error as error:
            storage_error(error, policy, "body")
        finally:
            if connection.in_transaction:
                connection.rollback()
