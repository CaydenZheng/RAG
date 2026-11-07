"""Real rollback-journal locks at conversation lookup, BEGIN and COMMIT."""

import asyncio
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, nullcontext
from types import SimpleNamespace

import pytest
from fastapi import Request


@pytest.fixture
def storage(temporary_sessions, monkeypatch):
    from config.settings import settings
    from src.security.request_authorization import RequestAuthorization

    monkeypatch.setattr(settings, "auth_enabled", True)
    temporary_sessions.initialize_ownership()
    authorization = RequestAuthorization(str(uuid.uuid4()), lambda: True, lambda: nullcontext(True))
    return temporary_sessions, authorization


def hold_reader(store):
    reader = sqlite3.connect(store._db_path)
    assert reader.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    reader.execute("BEGIN")
    reader.execute("SELECT * FROM conversations").fetchall()
    return reader


def observe_busy(store, monkeypatch, stage):
    busy = threading.Event()

    class ObservedConnection(sqlite3.Connection):
        def execute(self, sql, *args):
            try:
                return super().execute(sql, *args)
            except sqlite3.OperationalError as error:
                if stage == "begin" and sql == "BEGIN IMMEDIATE" and error.sqlite_errorcode == sqlite3.SQLITE_BUSY:
                    busy.set()
                raise

        def commit(self):
            try:
                return super().commit()
            except sqlite3.OperationalError as error:
                if stage == "commit" and error.sqlite_errorcode == sqlite3.SQLITE_BUSY:
                    busy.set()
                raise

    monkeypatch.setattr(store, "_connect", lambda: sqlite3.connect(store._db_path, factory=ObservedConnection))
    return busy


def test_existing_conversation_prepares_while_reader_is_open(storage):
    from src.infra.session_store import CommitPermit
    from src.security.request_authorization import bind_authorization

    store, auth = storage
    with bind_authorization(auth):
        expected = store.owned_session(auth.user_id, "existing", "agent", create=True)
        with closing(hold_reader(store)):
            assert store.owned_session(auth.user_id, "existing", "agent", create=True, can_commit=CommitPermit()) == expected


@pytest.mark.parametrize("stage", ["begin", "commit"])
def test_new_conversation_waits_for_short_lock_and_commits_once(storage, monkeypatch, stage):
    from src.infra.session_store import CommitPermit
    from src.security.request_authorization import bind_authorization

    store, auth = storage
    busy = observe_busy(store, monkeypatch, stage)
    blocker = sqlite3.connect(store._db_path) if stage == "begin" else hold_reader(store)
    if stage == "begin":
        blocker.execute("BEGIN IMMEDIATE")

    def create():
        with bind_authorization(auth):
            return store.owned_session(auth.user_id, "new", "rag", create=True, can_commit=CommitPermit())

    with closing(blocker), ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(create)
        try:
            assert busy.wait(3), "did not encounter the real SQLite lock"
        finally:
            blocker.rollback()
        assert result.result(timeout=3)
    with closing(sqlite3.connect(store._db_path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 1


def test_two_creators_recheck_after_acquiring_write_transaction(storage, monkeypatch):
    from src.infra.session_store import CommitPermit
    from src.security.request_authorization import bind_authorization

    store, auth = storage
    both_missing = threading.Barrier(2)

    class RacingConnection(sqlite3.Connection):
        def execute(self, sql, *args):
            if sql == "BEGIN IMMEDIATE" and not getattr(self, "arrived", False):
                self.arrived = True
                both_missing.wait(timeout=3)
            return super().execute(sql, *args)

    monkeypatch.setattr(store, "_connect", lambda: sqlite3.connect(store._db_path, factory=RacingConnection))

    def create():
        with bind_authorization(auth):
            return store.owned_session(auth.user_id, "same", "rag", create=True, can_commit=CommitPermit())

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(create) for _ in range(2)]
        assert futures[0].result(timeout=4) == futures[1].result(timeout=4)
    with closing(sqlite3.connect(store._db_path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 1


@pytest.mark.parametrize("stage", ["begin", "commit"])
def test_persistent_lock_has_bounded_storage_error_and_no_write(storage, monkeypatch, stage):
    from src.core.run_options import WorkspaceError
    from src.infra import session_store as stores
    from src.security.request_authorization import bind_authorization

    store, auth = storage
    monkeypatch.setattr(stores, "_CONVERSATION_WRITE_TIMEOUT_MS", 40)
    blocker = sqlite3.connect(store._db_path) if stage == "begin" else hold_reader(store)
    if stage == "begin":
        blocker.execute("BEGIN IMMEDIATE")

    def create():
        with bind_authorization(auth):
            store.owned_session(auth.user_id, "busy", "rag", create=True, can_commit=stores.CommitPermit())

    with closing(blocker), ThreadPoolExecutor(max_workers=1) as pool:
        try:
            with pytest.raises(WorkspaceError) as result:
                pool.submit(create).result(timeout=1)
            assert (result.value.code, result.value.status) == ("conversation_storage_busy", 503)
        finally:
            blocker.rollback()
    with closing(sqlite3.connect(store._db_path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0


@pytest.mark.parametrize("stage", ["begin", "commit"])
@pytest.mark.parametrize("interruption", ["cancel", "timeout", "logout", "revoke"])
def test_preparation_interrupted_while_locked_has_no_late_write_or_registration(storage, monkeypatch, stage, interruption):
    from src.api.conversations import begin_exchange
    from src.security import request_authorization as authority

    store, base_auth = storage
    valid = threading.Event()
    valid.set()
    auth = authority.RequestAuthorization(base_auth.user_id, valid.is_set, lambda: nullcontext(valid.is_set()), generation=True)
    busy = observe_busy(store, monkeypatch, stage)
    blocker = sqlite3.connect(store._db_path) if stage == "begin" else hold_reader(store)
    if stage == "begin":
        blocker.execute("BEGIN IMMEDIATE")
    request = Request({"type": "http", "state": {"user": SimpleNamespace(id=auth.user_id)}})

    async def run():
        deadline = asyncio.timeout(None)

        async def prepare():
            async with deadline:
                await begin_exchange(request, "interrupted", None, None, "rag", "question")

        with authority.bind_authorization(auth):
            task = asyncio.create_task(prepare())
            try:
                assert await asyncio.to_thread(busy.wait, 3)
                if interruption == "timeout":
                    deadline.reschedule(asyncio.get_running_loop().time())
                elif interruption == "cancel":
                    task.cancel()
                elif interruption == "logout":
                    valid.clear()
                else:
                    auth.revoked.set()
                expected = TimeoutError if interruption == "timeout" else asyncio.CancelledError if interruption == "cancel" else authority.AuthorizationRevoked
                with pytest.raises(expected):
                    await asyncio.wait_for(task, 1)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    try:
        # asyncio.run also joins the preparation worker while the lock is held.
        asyncio.run(run())
    finally:
        blocker.rollback()
        blocker.close()
    assert auth not in authority._RUNS.values()
    with closing(sqlite3.connect(store._db_path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0
