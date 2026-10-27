"""Real SQLite read locks must not turn revoked writes into committed history."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def commit_busy() -> threading.Event:
    return threading.Event()


@pytest.fixture
def commit_connections() -> list[sqlite3.Connection]:
    return []


@pytest.fixture
def commit_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    commit_busy: threading.Event, commit_connections: list[sqlite3.Connection],
) -> Iterator[tuple[Any, sqlite3.Connection, threading.Event]]:
    from src.infra.session_store import SessionStore

    database = tmp_path / "commit.db"
    store = SessionStore(str(database))
    store.add_turn("session", "user", "previous")
    attempted = threading.Event()

    class SignallingConnection(sqlite3.Connection):
        history_write = False

        def commit(self) -> None:
            if self.history_write:
                attempted.set()
            try:
                super().commit()
            except sqlite3.OperationalError as exc:
                if exc.sqlite_errorcode & 0xFF == sqlite3.SQLITE_BUSY:
                    commit_busy.set()
                raise

    def connect() -> sqlite3.Connection:
        connection = sqlite3.connect(
            database, timeout=1, factory=SignallingConnection, check_same_thread=False,
        )

        def trace(statement: str) -> None:
            if statement.startswith(("INSERT INTO sessions ", "DELETE FROM sessions ")):
                connection.history_write = True

        connection.set_trace_callback(trace)
        commit_connections.append(connection)
        return connection

    reader = sqlite3.connect(database)
    assert reader.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    monkeypatch.setattr(store, "_connect", connect)
    try:
        yield store, reader, attempted
    finally:
        reader.close()
        for connection in commit_connections:
            connection.close()


def _hold_reader(reader: sqlite3.Connection) -> None:
    reader.execute("BEGIN")
    reader.execute("SELECT * FROM sessions").fetchall()


@pytest.mark.parametrize("replace", [False, True])
def test_revocation_during_commit_contention_rolls_back(
    commit_database: tuple[Any, sqlite3.Connection, threading.Event], replace: bool,
) -> None:
    from src.infra.session_store import SessionTurn

    store, reader, attempted = commit_database
    original = store.read_snapshot("session")
    revoked = threading.Event()
    _hold_reader(reader)

    def save() -> bool:
        pending = [SessionTurn(role="assistant", content="late answer")]
        if replace:
            return store.replace_history_if_revision(
                "session", original.revision, pending,
                can_commit=lambda: not revoked.is_set(),
            )
        return store.append_turns_if_epoch(
            "session", original.revision[0], pending,
            can_commit=lambda: not revoked.is_set(),
        )

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(save)
        try:
            assert attempted.wait(1), "write never reached SQLite COMMIT"
            revoked.set()
            # A guarded COMMIT must notice revocation without needing the
            # reader to release its real shared lock first.
            assert future.result(timeout=0.5) is False
        finally:
            reader.rollback()
    assert store.read_snapshot("session") == original


@pytest.mark.parametrize("replace", [False, True])
@pytest.mark.parametrize("outcome", ["timeout", "cancel"])
def test_async_memory_has_no_late_commit_after_reader_unlocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    commit_database: tuple[Any, sqlite3.Connection, threading.Event],
    replace: bool, outcome: str,
) -> None:
    from src.agent.memory import (
        MemoryConfig,
        MemoryManager,
        MemoryTurn,
        MemoryUpdateStatus,
    )

    store, reader, attempted = commit_database
    original = store.read_snapshot("session")
    memory = MemoryManager(
        tmp_path / "memory", store=store,
        config=MemoryConfig(compress_trigger_turns=3 if replace else 100, compress_keep_recent=1),
    )
    summarizing, resume, finished = threading.Event(), threading.Event(), threading.Event()
    add_turns = memory.add_turns

    def save(*args: Any, **kwargs: Any) -> MemoryUpdateStatus:
        try:
            return add_turns(*args, **kwargs)
        finally:
            finished.set()

    def summarize(*_args: Any) -> str:
        # Let the compression claim commit before introducing the read lock;
        # the target is the final history replacement, not claim acquisition.
        summarizing.set()
        assert resume.wait(2)
        return "summary"

    monkeypatch.setattr(memory, "add_turns", save)
    monkeypatch.setattr(memory, "_llm_compress", summarize)

    async def exercise() -> None:
        if not replace:
            _hold_reader(reader)
        task = asyncio.create_task(memory.add_turns_async(
            "session", [MemoryTurn(role="user", content="question"),
                        MemoryTurn(role="assistant", content="late answer")],
            expected_epoch=original.revision[0], deadline=time.monotonic() + 0.25,
        ))
        try:
            if replace:
                assert await asyncio.to_thread(summarizing.wait, 1)
                _hold_reader(reader)
                resume.set()
            assert await asyncio.to_thread(attempted.wait, 1)
            if outcome == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                try:
                    status = await asyncio.wait_for(task, 0.3)
                except TimeoutError:
                    pass
                else:
                    assert status is MemoryUpdateStatus.STALE_DISCARDED
        finally:
            # This is the old failure order: revoke first, then release the
            # reader. The worker must not resurrect the expired interaction.
            reader.rollback()
            resume.set()
            await asyncio.gather(task, return_exceptions=True)
            assert await asyncio.to_thread(finished.wait, 1)

    asyncio.run(exercise())
    assert store.read_snapshot("session") == original


@pytest.mark.parametrize("replace", [False, True])
@pytest.mark.parametrize("guarded", [False, True])
def test_valid_guard_retries_contention_and_then_commits(
    commit_database: tuple[Any, sqlite3.Connection, threading.Event], replace: bool,
    guarded: bool, commit_busy: threading.Event,
    commit_connections: list[sqlite3.Connection],
) -> None:
    from src.infra.session_store import SessionTurn

    store, reader, attempted = commit_database
    snapshot = store.read_snapshot("session")
    _hold_reader(reader)
    checks = 0

    def permitted() -> bool:
        nonlocal checks
        checks += 1
        return True

    def save() -> bool:
        pending = [SessionTurn(role="assistant", content="answer")]
        if replace:
            return store.replace_history_if_revision(
                "session", snapshot.revision, pending,
                can_commit=permitted if guarded else None,
            )
        return store.append_turns_if_epoch(
            "session", snapshot.revision[0], pending,
            can_commit=permitted if guarded else None,
        )

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(save)
        try:
            assert (commit_busy if guarded else attempted).wait(1)
        finally:
            reader.rollback()
        assert future.result(timeout=1)
    expected = ["answer"] if replace else ["previous", "answer"]
    assert [turn["content"] for turn in store.get_history("session")] == expected
    if guarded:
        assert checks >= 2, "busy COMMIT retry did not recheck permission"
    assert all(connection.execute("PRAGMA busy_timeout").fetchone()[0] == 1000
               for connection in commit_connections)


@pytest.mark.parametrize("replace", [False, True])
@pytest.mark.parametrize("outcome", ["cancel", "timeout"])
def test_preempted_memory_commit_serializes_cancellation_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    commit_database: tuple[Any, sqlite3.Connection, threading.Event],
    replace: bool, outcome: str,
) -> None:
    """Keep the original safety assertion, but pause AFTER permission checks."""
    store, reader, attempted = commit_database
    reader_released: threading.Event = threading.Event()
    connection_type = type(store._connect())

    class Reader:
        def execute(self, *args: Any, **kwargs: Any) -> Any:
            return reader.execute(*args, **kwargs)

        def rollback(self) -> None:
            reader.rollback()
            reader_released.set()

    def paused_commit(connection: Any) -> None:
        if connection.history_write:
            attempted.set()
            # Model preemption immediately before entering SQLite. Bounded so
            # a correctly serialized cancel can wait for this one attempt, but
            # must never require the caller to unlock the reader first.
            reader_released.wait(0.5)
        sqlite3.Connection.commit(connection)

    monkeypatch.setattr(connection_type, "commit", paused_commit)
    test_async_memory_has_no_late_commit_after_reader_unlocks(
        tmp_path, monkeypatch, (store, Reader(), attempted), replace, outcome,
    )


@pytest.mark.parametrize("replace", [False, True])
def test_admitted_commit_finishes_before_revocation_is_confirmed(
    commit_database: tuple[Any, sqlite3.Connection, threading.Event],
    monkeypatch: pytest.MonkeyPatch, replace: bool,
) -> None:
    from src.infra.session_store import CommitPermit, SessionTurn

    store, _, attempted = commit_database
    original = store.read_snapshot("session")
    permit: CommitPermit = CommitPermit()
    resume: threading.Event = threading.Event()
    committed: threading.Event = threading.Event()
    revoking: threading.Event = threading.Event()
    confirmed: threading.Event = threading.Event()
    connection_type = type(store._connect())

    def paused_commit(connection: Any) -> None:
        if connection.history_write:
            attempted.set()
            assert resume.wait(2)
        sqlite3.Connection.commit(connection)
        if connection.history_write:
            committed.set()

    def save() -> bool:
        turns = [SessionTurn("assistant", "accepted answer")]
        if replace:
            return store.replace_history_if_revision(
                "session", original.revision, turns, can_commit=permit,
            )
        return store.append_turns_if_epoch(
            "session", original.revision[0], turns, can_commit=permit,
        )

    def revoke() -> bool:
        revoking.set()
        permit.revoke()
        confirmed.set()
        return committed.is_set()

    monkeypatch.setattr(connection_type, "commit", paused_commit)
    with ThreadPoolExecutor(max_workers=2) as executor:
        writer = executor.submit(save)
        try:
            assert attempted.wait(1)
            revoker = executor.submit(revoke)
            assert revoking.wait(1)
            assert not confirmed.wait(0.05)
        finally:
            resume.set()
        assert writer.result(timeout=1)
        assert revoker.result(timeout=1)
    assert not permit()
    assert store.get_history("session")[-1]["content"] == "accepted answer"


@pytest.mark.parametrize("replace", [False, True])
def test_commit_permit_is_released_during_busy_retry_wait(
    commit_database: tuple[Any, sqlite3.Connection, threading.Event],
    monkeypatch: pytest.MonkeyPatch, replace: bool,
) -> None:
    from src.infra import session_store as storage_module
    from src.infra.session_store import CommitPermit, SessionTurn

    store, reader, _ = commit_database
    original = store.read_snapshot("session")
    permit: CommitPermit = CommitPermit()
    waiting: threading.Event = threading.Event()
    resume: threading.Event = threading.Event()
    wait_expired: threading.Event = threading.Event()

    def retry_wait(_seconds: float) -> None:
        waiting.set()
        if not resume.wait(2):
            wait_expired.set()

    def save() -> bool:
        turns = [SessionTurn("assistant", "must not save")]
        if replace:
            return store.replace_history_if_revision(
                "session", original.revision, turns, can_commit=permit,
            )
        return store.append_turns_if_epoch(
            "session", original.revision[0], turns, can_commit=permit,
        )

    monkeypatch.setattr(storage_module.time, "sleep", retry_wait)
    _hold_reader(reader)
    with ThreadPoolExecutor(max_workers=1) as executor:
        writer = executor.submit(save)
        try:
            assert waiting.wait(1)
            permit.revoke()
            assert not wait_expired.is_set(), "revoke waited for retry sleep"
            resume.set()
            assert writer.result(timeout=1) is False
        finally:
            resume.set()
            reader.rollback()
    assert store.read_snapshot("session") == original


def test_memory_cancellation_does_not_wait_for_llm_compression(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    commit_database: tuple[Any, sqlite3.Connection, threading.Event],
) -> None:
    from src.agent.memory import MemoryConfig, MemoryManager, MemoryTurn

    store, _, _ = commit_database
    original = store.read_snapshot("session")
    memory = MemoryManager(
        tmp_path / "memory", store=store,
        config=MemoryConfig(compress_trigger_turns=3, compress_keep_recent=1),
    )
    summarizing: threading.Event = threading.Event()
    resume: threading.Event = threading.Event()
    finished: threading.Event = threading.Event()
    compression_expired: threading.Event = threading.Event()
    add_turns = memory.add_turns

    def save(*args: Any, **kwargs: Any) -> Any:
        try:
            return add_turns(*args, **kwargs)
        finally:
            finished.set()

    def summarize(*_args: Any) -> str:
        summarizing.set()
        if not resume.wait(2):
            compression_expired.set()
        return "summary"

    monkeypatch.setattr(memory, "add_turns", save)
    monkeypatch.setattr(memory, "_llm_compress", summarize)

    async def exercise() -> None:
        task = asyncio.create_task(memory.add_turns_async(
            "session", [MemoryTurn("user", "question"), MemoryTurn("assistant", "answer")],
        ))
        try:
            assert await asyncio.to_thread(summarizing.wait, 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not compression_expired.is_set(), "cancel waited for compression"
            assert not finished.is_set()
        finally:
            resume.set()
            await asyncio.gather(task, return_exceptions=True)
            assert await asyncio.to_thread(finished.wait, 1)

    asyncio.run(exercise())
    assert store.read_snapshot("session") == original


def test_guarded_commit_preserves_sqlite_busy_wait_cap(
    commit_database: tuple[Any, sqlite3.Connection, threading.Event],
    commit_connections: list[sqlite3.Connection], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.infra.session_store import SessionTurn

    store, reader, _ = commit_database
    snapshot = store.read_snapshot("session")
    connect = store._connect

    def short_wait_connection() -> sqlite3.Connection:
        connection = connect()
        connection.execute("PRAGMA busy_timeout = 40")
        return connection

    monkeypatch.setattr(store, "_connect", short_wait_connection)
    _hold_reader(reader)
    started = time.monotonic()
    try:
        with pytest.raises(sqlite3.OperationalError) as failure:
            store.append_turns_if_epoch(
                "session", snapshot.revision[0],
                [SessionTurn(role="assistant", content="never committed")],
                can_commit=lambda: True,
            )
        assert failure.value.sqlite_errorcode & 0xFF == sqlite3.SQLITE_BUSY
        assert time.monotonic() - started < 0.5
        writer = commit_connections[-1]
        assert writer.in_transaction is False
        assert writer.execute("PRAGMA busy_timeout").fetchone()[0] == 40
    finally:
        reader.rollback()
    assert store.read_snapshot("session") == snapshot


def test_non_busy_commit_error_is_not_retried(
    commit_database: tuple[Any, sqlite3.Connection, threading.Event],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.infra.session_store import SessionTurn

    store, _, _ = commit_database
    snapshot = store.read_snapshot("session")
    connection_type = type(store._connect())
    attempts = 0

    def fail_commit(connection: sqlite3.Connection) -> None:
        nonlocal attempts
        if connection.total_changes:
            attempts += 1
            error = sqlite3.OperationalError("synthetic non-busy commit failure")
            error.sqlite_errorcode = sqlite3.SQLITE_LOCKED
            raise error
        sqlite3.Connection.commit(connection)

    monkeypatch.setattr(connection_type, "commit", fail_commit)
    with pytest.raises(sqlite3.OperationalError):
        store.append_turns_if_epoch(
            "session", snapshot.revision[0],
            [SessionTurn(role="assistant", content="never committed")],
            can_commit=lambda: True,
        )
    assert attempts == 1
    assert store.read_snapshot("session") == snapshot
