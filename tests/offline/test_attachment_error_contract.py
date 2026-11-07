"""Resource error classification and cleanup at real HTTP/runtime seams."""
import asyncio
import sqlite3
import uuid
from types import SimpleNamespace

import pytest

from tests.offline.test_account_ownership import accounts as accounts
from tests.offline.test_attachment_transactions import attachments as attachments


def cannot_open():
    error = sqlite3.OperationalError("synthetic database open failure")
    error.sqlite_errorcode = sqlite3.SQLITE_CANTOPEN
    raise error


@pytest.mark.parametrize("resource", ["attachments", "conversations"])
def test_connection_failure_keeps_resource_error(accounts, monkeypatch, resource):
    client = accounts.browsers["alice"]
    cid = str(uuid.uuid4())
    assert client.post("/conversations", json={"id": cid, "mode": "rag"}).status_code == 201
    monkeypatch.setattr(accounts.store, "_connect", cannot_open)
    if resource == "attachments":
        response = client.get(f"/conversations/{cid}/attachments")
        expected = "attachment_unavailable"
    else:
        response = client.post("/conversations", json={"id": str(uuid.uuid4()), "mode": "rag"})
        expected = "conversation_storage_unavailable"
    assert client.get("/auth/me").status_code == 200
    print(resource, response.status_code, response.json())
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == expected


def test_shared_write_classifies_connection_failure():
    from src.core.run_options import WorkspaceError
    from src.infra.sqlite_transactions import WritePolicy, write_transaction
    with pytest.raises(WorkspaceError, match="attachment_unavailable"):
        with write_transaction(cannot_open, WritePolicy(.01, .01, "attachment_storage_busy", "attachment_unavailable")):
            pytest.fail("unreachable")


@pytest.mark.parametrize("failure", ["start", "ipc", "exit"])
def test_process_failure_is_not_invalid_document(attachments, monkeypatch, failure):
    from src.core import attachment_runtime as runtime
    from src.infra import document_parser
    from src.security.request_authorization import bind_authorization

    store, auth = attachments
    data = b"valid synthetic text"
    assert document_parser.extract_document(data, "txt")["text"]

    released = []

    def start():
        if failure == "start":
            raise OSError("PRIVATE process resource failure")

    def receive():
        raise EOFError("PRIVATE IPC failure")

    context = SimpleNamespace(
        Pipe=lambda **kwargs: (
            SimpleNamespace(close=lambda: released.append("reader"), poll=lambda: failure == "ipc", recv=receive),
            SimpleNamespace(close=lambda: released.append("writer")),
        ),
        Process=lambda **kwargs: SimpleNamespace(start=start, close=lambda: released.append("process"),
                                                is_alive=lambda: False, exitcode=7,
                                                join=lambda *args, **kwargs: released.append("joined")),
    )
    monkeypatch.setattr(document_parser.multiprocessing, "get_context", lambda *_: context)

    async def run():
        with bind_authorization(auth):
            try:
                item = await runtime.accept(store, "existing", str(uuid.uuid4()), "valid.txt", "txt", data)
                await asyncio.gather(*tuple(runtime._TASKS.values()), return_exceptions=True)
                await asyncio.gather(*tuple(runtime._FINALIZING), return_exceptions=True)
                result = store.read("existing", item["id"])
                assert result["state"] == "failed"
                assert result["error_code"] == "document_processing_failed"
                assert not result["cleanup_pending"]
                assert not list(store.root.iterdir())
            finally:
                await runtime.close()
    asyncio.run(run())
    assert released.count("process") == released.count("reader") == 1
    assert "writer" in released
    assert ("joined" in released) == (failure != "start")
    assert not runtime._TASKS and not runtime._FINALIZING and not runtime._PENDING
    assert not document_parser._PARSING_OWNERS


@pytest.mark.parametrize("code", ["attachment_storage_busy", "attachment_unavailable"])
def test_reconciliation_preserves_failure_and_recovers(attachments, monkeypatch, code):
    from src.core import attachment_runtime as runtime
    from src.core.run_options import WorkspaceError
    store, _ = attachments
    aid = str(uuid.uuid4())

    def unavailable(*args):
        raise WorkspaceError(code, 503)

    async def run():
        try:
            with monkeypatch.context() as patch:
                patch.setattr(store, "interrupt", unavailable)
                await runtime._defer_interrupted(store, "existing", aid, "parse_interrupted")
                with pytest.raises(WorkspaceError, match=code):
                    await runtime.reconcile(store, "existing")
                assert (str(store.root), aid) in runtime._PENDING
            await runtime.reconcile(store, "existing")
            assert (str(store.root), aid) not in runtime._PENDING
        finally:
            await runtime.close()
    asyncio.run(run())


@pytest.mark.parametrize("operation", ["read", "write", "conversation"])
@pytest.mark.parametrize("stage", ["factory_init", "access_init", "close"])
def test_connection_setup_and_close_are_classified_and_released(attachments, monkeypatch, operation, stage):
    from src.core.run_options import WorkspaceError
    from src.infra.attachment_store import ATTACHMENT_WRITE_POLICY
    from src.security.request_authorization import bind_authorization

    store, auth = attachments
    connect = sqlite3.connect
    opened, closed = [], []

    def fail():
        error = sqlite3.OperationalError("PRIVATE initialization/close failure")
        error.sqlite_errorcode = sqlite3.SQLITE_IOERR
        raise error

    class Connection(sqlite3.Connection):
        def execute(self, sql, *args):
            if (stage == "factory_init" and "foreign_keys" in sql) or (stage == "access_init" and sql == "PRAGMA busy_timeout=0"):
                fail()
            return super().execute(sql, *args)

        def close(self):
            closed.append(self)
            super().close()
            if stage == "close":
                fail()

    def tracked_connect(*args, **kwargs):
        connection = connect(*args, **kwargs, factory=Connection)
        opened.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", tracked_connect)
    expected = "conversation_storage_unavailable" if operation == "conversation" else "attachment_unavailable"
    with bind_authorization(auth), pytest.raises(WorkspaceError, match=expected) as result:
        if operation == "read":
            store.read("existing", str(uuid.uuid4()))
        elif operation == "write":
            with store.store.write_transaction(ATTACHMENT_WRITE_POLICY):
                pass
        else:
            store.store.owned_session(auth.user_id, "existing", "rag", create=False)
    assert "PRIVATE" not in str(result.value)
    assert opened and opened == closed
    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")


def test_auth_database_failure_retains_auth_error(accounts, monkeypatch):
    monkeypatch.setattr(accounts.service, "_connection", cannot_open)
    response = accounts.browsers["alice"].get("/auth/me")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "auth_unavailable"


def test_worker_unknown_failure_is_processing_error_and_log_is_redacted(monkeypatch):
    from loguru import logger

    from src.infra import document_parser

    def fail(*args):
        raise RuntimeError("PRIVATE document contents")

    monkeypatch.setattr(document_parser, "extract_document", fail)
    sent, closed, logs = [], [], []
    channel = SimpleNamespace(send=sent.append, close=lambda: closed.append(True))
    sink = logger.add(lambda message: logs.append(str(message)))
    try:
        document_parser._worker(b"PRIVATE", "txt", channel)
    finally:
        logger.remove(sink)
    assert sent == [{"code": "document_processing_failed", "status": 503}]
    assert closed == [True]
    assert "RuntimeError" in "".join(logs) and "PRIVATE" not in "".join(logs)


@pytest.mark.parametrize("suffix,data", [
    ("txt", b"\xff"), ("txt", b"  "), ("pdf", b"%PDF-1.7\nbroken"),
    ("docx", b"not a zip"),
])
def test_known_bad_documents_remain_input_errors(suffix, data):
    from src.core.run_options import WorkspaceError
    from src.infra.document_parser import extract_document

    with pytest.raises(WorkspaceError, match="document_invalid"):
        extract_document(data, suffix)
