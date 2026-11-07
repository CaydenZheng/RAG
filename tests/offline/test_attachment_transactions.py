"""Attachment side effects and real SQLite lock contention across the lifecycle."""

import asyncio
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, nullcontext

import pytest


@pytest.fixture
def attachments(temporary_sessions, monkeypatch):
    from config.settings import settings
    from src.infra.attachment_store import AttachmentStore
    from src.security.request_authorization import (
        RequestAuthorization,
        bind_authorization,
    )

    monkeypatch.setattr(settings, "auth_enabled", True)
    temporary_sessions.initialize_ownership()
    auth = RequestAuthorization(str(uuid.uuid4()), lambda: True, lambda: nullcontext(True))
    with bind_authorization(auth):
        temporary_sessions.owned_session(auth.user_id, "existing", "rag", create=True)
    return AttachmentStore(temporary_sessions), auth


def locked_database(store, stage):
    lock = sqlite3.connect(store.store._db_path)
    lock.execute("BEGIN IMMEDIATE" if stage == "begin" else "BEGIN")
    lock.execute("SELECT * FROM conversations").fetchall()
    return lock


def observe_busy(store, monkeypatch):
    busy = threading.Event()

    class Observed(sqlite3.Connection):
        def execute(self, sql, *args):
            try:
                return super().execute(sql, *args)
            except sqlite3.OperationalError as error:
                if error.sqlite_errorcode == sqlite3.SQLITE_BUSY:
                    busy.set()
                raise

        def commit(self):
            try:
                return super().commit()
            except sqlite3.OperationalError as error:
                if error.sqlite_errorcode == sqlite3.SQLITE_BUSY:
                    busy.set()
                raise

    monkeypatch.setattr(store.store, "_connect", lambda: sqlite3.connect(store.store._db_path, factory=Observed))
    return busy


@pytest.mark.parametrize("stage", ["begin", "commit"])
@pytest.mark.parametrize("operation", ["accept", "finish", "delete", "interrupt", "recover", "cleanup"])
def test_short_lock_recovers_without_replaying_side_effects(attachments, monkeypatch, operation, stage):
    from src.infra.session_store import CommitPermit
    from src.security.request_authorization import bind_authorization

    store, auth = attachments
    with bind_authorization(auth):
        item, _ = store.accept("existing", str(uuid.uuid4()), "file.txt", "txt", b"synthetic")
    if operation == "cleanup":
        with closing(sqlite3.connect(store.store._db_path)) as connection, connection:
            connection.execute("UPDATE conversation_attachments SET state='failed',cleanup=1")
    admitted = []
    busy = observe_busy(store, monkeypatch)
    lock = locked_database(store, stage)

    def perform():
        with bind_authorization(auth):
            if operation == "accept":
                return store.accept("existing", str(uuid.uuid4()), "new.txt", "txt", b"new", can_commit=CommitPermit(), admit=lambda: admitted.append(True))
            if operation == "finish":
                return store.finish("existing", item["id"], item["id"], {"text": "parsed", "pages": []})
            if operation == "delete":
                return store.delete("existing", item["id"])
            if operation == "interrupt":
                return store.interrupt(item["id"], item["id"])
            return getattr(store, operation)()

    with closing(lock), ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(perform)
        try:
            assert busy.wait(2), "operation did not encounter the real lock"
        finally:
            lock.rollback()
        result = pending.result(timeout=3)
    with bind_authorization(auth):
        if operation == "accept":
            assert result[1] and admitted == [True]
            assert len(store.listing("existing")) == 2
        elif operation == "finish":
            assert store.read("existing", item["id"])["state"] == "ready"
            assert store._path(item["id"], "text").read_text(encoding="utf8") == "parsed"
        else:
            assert not store._path(item["id"], "raw").exists()
            with closing(sqlite3.connect(store.store._db_path)) as connection:
                state, cleanup = connection.execute("SELECT state,cleanup FROM conversation_attachments WHERE id=?", (item["id"],)).fetchone()
                assert state in {"deleted", "failed"} and cleanup == 0


def test_existing_upload_can_be_reconciled_under_reader_without_admitting_again(attachments):
    from src.infra.session_store import CommitPermit
    from src.security.request_authorization import bind_authorization

    store, auth = attachments
    upload = str(uuid.uuid4())
    with bind_authorization(auth):
        item, _ = store.accept("existing", upload, "file.txt", "txt", b"same")
        with closing(locked_database(store, "commit")):
            result, created = store.accept("existing", upload, "file.txt", "txt", b"same", can_commit=CommitPermit(), admit=lambda: pytest.fail("duplicate admission"))
        assert not created and result["id"] == item["id"]


@pytest.mark.parametrize("operation", ["accept", "finish"])
def test_revocation_during_commit_removes_uncommitted_file(attachments, monkeypatch, operation):
    from src.infra.session_store import CommitPermit
    from src.security.request_authorization import (
        AuthorizationRevoked,
        bind_authorization,
    )

    store, auth = attachments
    with bind_authorization(auth):
        item, _ = store.accept("existing", str(uuid.uuid4()), "old.txt", "txt", b"old")
    busy = observe_busy(store, monkeypatch)
    lock = locked_database(store, "commit")

    def perform():
        with bind_authorization(auth):
            if operation == "accept":
                store.accept("existing", str(uuid.uuid4()), "new.txt", "txt", b"new", can_commit=CommitPermit())
            else:
                store.finish("existing", item["id"], item["id"], {"text": "late", "pages": []})

    with closing(lock), ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(perform)
        try:
            assert busy.wait(2)
            auth.revoked.set()
            with pytest.raises(AuthorizationRevoked):
                pending.result(timeout=1)
        finally:
            lock.rollback()
    assert sorted(path.name for path in store.root.iterdir()) == [item["id"] + ".raw"]
    with closing(sqlite3.connect(store.store._db_path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM conversation_attachments").fetchone()[0] == 1
        assert connection.execute("SELECT state FROM conversation_attachments").fetchone()[0] == "parsing"


@pytest.mark.parametrize("stage", ["begin", "commit"])
def test_persistent_contention_is_attachment_busy_with_no_file_or_reservation_leak(attachments, monkeypatch, stage):
    from src.core import attachment_runtime as runtime
    from src.core.run_options import WorkspaceError
    from src.infra import attachment_store
    from src.infra.document_parser import release_parser, reserve_parser
    from src.infra.sqlite_transactions import WritePolicy
    from src.security.request_authorization import bind_authorization

    store, auth = attachments
    monkeypatch.setattr(attachment_store, "ATTACHMENT_WRITE_POLICY", WritePolicy(.04, .04, "attachment_storage_busy", "attachment_unavailable"))
    lock = locked_database(store, stage)

    async def run():
        with bind_authorization(auth), pytest.raises(WorkspaceError, match="attachment_storage_busy"):
            await runtime.accept(store, "existing", str(uuid.uuid4()), "file.txt", "txt", b"data")
        await runtime.close()
        assert not runtime._TASKS and not runtime._ACCEPTING and not runtime._FINALIZING
        with bind_authorization(auth):
            release_parser(reserve_parser())

    try:
        asyncio.run(run())
    finally:
        lock.rollback()
        lock.close()
    assert not store.root.exists() or list(store.root.iterdir()) == []
    with closing(sqlite3.connect(store.store._db_path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM conversation_attachments").fetchone()[0] == 0


@pytest.mark.parametrize("stage", ["begin", "commit"])
def test_shutdown_joins_accept_worker_before_releasing_reservation(attachments, monkeypatch, stage):
    from src.core import attachment_runtime as runtime
    from src.infra.document_parser import release_parser, reserve_parser
    from src.security.request_authorization import bind_authorization

    store, auth = attachments
    busy = observe_busy(store, monkeypatch)
    lock = locked_database(store, stage)

    async def run():
        with bind_authorization(auth):
            pending = asyncio.create_task(runtime.accept(store, "existing", str(uuid.uuid4()), "file.txt", "txt", b"data"))
            try:
                assert await asyncio.to_thread(busy.wait, 2)
                await asyncio.wait_for(runtime.close(), 1)
                with pytest.raises(asyncio.CancelledError):
                    await pending
                release_parser(reserve_parser())
                assert not runtime._TASKS and not runtime._ACCEPTING
            finally:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    try:
        asyncio.run(run())
    finally:
        lock.rollback()
        lock.close()
    assert not store.root.exists() or list(store.root.iterdir()) == []
    with closing(sqlite3.connect(store.store._db_path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM conversation_attachments").fetchone()[0] == 0


@pytest.mark.parametrize("cancel", [False, True])
def test_parser_finish_busy_or_cancelled_is_reconcilable_and_never_ready_late(attachments, monkeypatch, cancel):
    from src.core import attachment_runtime as runtime
    from src.infra import attachment_store
    from src.infra.document_parser import release_parser, reserve_parser
    from src.infra.sqlite_transactions import WritePolicy
    from src.security.request_authorization import bind_authorization

    store, auth = attachments
    monkeypatch.setattr(attachment_store, "ATTACHMENT_WRITE_POLICY", WritePolicy(.05, .05, "attachment_storage_busy", "attachment_unavailable"))
    busy = observe_busy(store, monkeypatch)

    async def run():
        ready = asyncio.Event()

        async def parser(*args, **kwargs):
            await ready.wait()
            return {"text": "valid text", "pages": []}

        monkeypatch.setattr(runtime, "parse_document", parser)
        with bind_authorization(auth):
            item = await runtime.accept(store, "existing", str(uuid.uuid4()), "file.txt", "txt", b"data")
            lock = locked_database(store, "commit")
            try:
                ready.set()
                assert await asyncio.to_thread(busy.wait, 2)
                if cancel:
                    await runtime.close()
                else:
                    await asyncio.gather(*tuple(runtime._TASKS.values()), return_exceptions=True)
                    await asyncio.gather(*tuple(runtime._FINALIZING), return_exceptions=True)
                assert (str(store.root), item["id"]) in runtime._PENDING
                assert not store._path(item["id"], "text").exists()
                release_parser(reserve_parser())
            finally:
                lock.rollback()
                lock.close()
            await runtime.reconcile(store, "existing")
            result = store.read("existing", item["id"])
            assert result["state"] == "failed"
            assert result["error_code"] == ("parse_interrupted" if cancel else "attachment_storage_busy")
            assert not result["cleanup_pending"]
            assert list(store.root.iterdir()) == []
            assert not runtime._TASKS and not runtime._FINALIZING and not runtime._PENDING
            await runtime.close()

    asyncio.run(run())
