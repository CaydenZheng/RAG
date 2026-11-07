"""Request preparation must yield while SQLite is busy and retain context."""

import asyncio
import inspect
import sqlite3
import threading
import uuid
from contextlib import closing, nullcontext
from types import SimpleNamespace

import pytest
from fastapi import Request


@pytest.fixture(autouse=True)
def prepare_storage(temporary_sessions, monkeypatch):
    import tiktoken

    monkeypatch.setattr(tiktoken, "get_encoding", lambda _: SimpleNamespace(encode=lambda text: list(text)))
    temporary_sessions.initialize_ownership()


def test_preparation_does_not_block_heartbeat(temporary_sessions, monkeypatch):
    from config.settings import settings
    from src.api.conversations import begin_exchange
    from src.security.request_authorization import (
        RequestAuthorization,
        bind_authorization,
    )

    monkeypatch.setattr(settings, "auth_enabled", True)
    owner = "11111111-1111-4111-8111-111111111111"
    authorization = RequestAuthorization(owner, lambda: True, lambda: nullcontext(True))
    locked = threading.Event()
    heartbeat = threading.Event()
    released_by_heartbeat = []

    def writer():
        with sqlite3.connect(temporary_sessions._db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            locked.set()
            # A safety watchdog only: the assertion is on ordering, not latency.
            released_by_heartbeat.append(heartbeat.wait(3))
            connection.rollback()

    worker = threading.Thread(target=writer)
    worker.start()
    assert locked.wait(3)
    request = Request({"type": "http", "state": {"user": SimpleNamespace(id=owner)}})

    async def run():
        async def beat():
            heartbeat.set()

        with bind_authorization(authorization):
            pulse = asyncio.create_task(beat())
            try:
                result = begin_exchange(request, "session", None, None, "rag", "question")
                if inspect.isawaitable(result):
                    await result
            except sqlite3.OperationalError:
                # A documented fail-fast busy response is also valid.
                pass
            finally:
                await pulse

    try:
        asyncio.run(run())
    finally:
        heartbeat.set()
        worker.join(5)
    assert released_by_heartbeat == [True]


@pytest.mark.parametrize("endpoint", ["query", "query_stream", "agent_chat", "agent_chat_stream"])
@pytest.mark.parametrize("existing_reader", [False, True])
def test_all_endpoints_bind_prepared_context(temporary_sessions, monkeypatch, endpoint, existing_reader):
    import app as api
    from config.settings import settings
    from src.api.conversations import begin_exchange
    from src.api.schemas import AgentChatRequest, QueryRequest
    from src.core.run_options import (
        AttachmentSnapshot,
        RunOptions,
        SkillSnapshot,
        current_run_options,
    )
    from src.infra.attachment_store import AttachmentStore
    from src.infra.exchange_context import completed_model, exchange_context
    from src.infra.skill_store import SkillStore
    from src.security.request_authorization import (
        RequestAuthorization,
        bind_authorization,
    )

    monkeypatch.setattr(settings, "auth_enabled", True)
    owner = str(uuid.uuid4())
    turn = str(uuid.uuid4())
    attachment = AttachmentSnapshot(str(uuid.uuid4()), "sample.txt", "checksum", "evidence")
    skill = SkillSnapshot("example", "Example", "v1", "guidance")
    options = RunOptions("pro", "test-pro", (skill,))
    monkeypatch.setattr(SkillStore, "snapshot", lambda *a, **kw: options)
    monkeypatch.setattr(AttachmentStore, "snapshot", lambda *a, **kw: (attachment,))
    request = Request({"type": "http", "state": {"user": SimpleNamespace(id=owner), "request_id": "test"}})
    authorization = RequestAuthorization(owner, lambda: True, lambda: nullcontext(True), generation=True)

    class Prepared(Exception):
        pass

    async def checked_begin(*args, **kwargs):
        result = await begin_exchange(*args, **kwargs)
        assert result.storage_id
        assert current_run_options.get() == RunOptions("pro", "test-pro", (skill,), (attachment,))
        context = exchange_context.get()
        assert context.client_turn_id == turn
        assert context.question == "question"
        assert context.requested_model == "pro"
        assert context.skills == ({"id": "example", "name": "Example", "version": "v1"},)
        assert context.attachments[0]["checksum"] == "checksum"
        assert completed_model.get() is None
        raise Prepared

    monkeypatch.setattr(api, "begin_exchange", checked_begin)

    async def run():
        with bind_authorization(authorization), pytest.raises(Prepared):
            if existing_reader:
                temporary_sessions.owned_session(owner, "session", "agent" if endpoint.startswith("agent") else "rag", create=True)
            with closing(sqlite3.connect(temporary_sessions._db_path)) as reader:
                if existing_reader:
                    reader.execute("BEGIN")
                    reader.execute("SELECT * FROM conversations").fetchall()
                kwargs = {"session_id": "session", "client_turn_id": turn, "model": "pro", "attachment_ids": [attachment.id]}
                if endpoint == "query_stream":
                    await api.query_stream(request, query="question", **kwargs)
                elif endpoint == "query":
                    await api.query(QueryRequest(query="question", **kwargs), request)
                else:
                    await getattr(api, endpoint)(AgentChatRequest(message="question", **kwargs), request)

    asyncio.run(run())


@pytest.mark.parametrize("stage", ["create", "commit", "register"])
@pytest.mark.parametrize("interruption", ["cancel", "timeout", "revoke"])
def test_interrupted_preparation_cannot_commit_or_register_late(
    temporary_sessions, monkeypatch, stage, interruption,
):
    from config.settings import settings
    from src.api import client_identity
    from src.api.conversations import begin_exchange
    from src.security import request_authorization as auth

    monkeypatch.setattr(settings, "auth_enabled", True)
    owner = str(uuid.uuid4())
    request = Request({"type": "http", "state": {"user": SimpleNamespace(id=owner)}})
    authorization = auth.RequestAuthorization(owner, lambda: True, lambda: nullcontext(True), generation=True)
    release = threading.Event()
    finished = threading.Event()

    async def run():
        entered = asyncio.Event()
        loop = asyncio.get_running_loop()
        from src.infra.sqlite_transactions import SQLiteWrite

        target, name = (client_identity, "register_generation") if stage == "register" else (
            (temporary_sessions, "owned_session") if stage == "create" else (SQLiteWrite, "commit")
        )
        original = getattr(target, name)

        def paused(*args, **kwargs):
            loop.call_soon_threadsafe(entered.set)
            try:
                assert release.wait(5)
                return original(*args, **kwargs)
            finally:
                finished.set()

        monkeypatch.setattr(target, name, paused)
        deadline = asyncio.timeout(None)

        async def prepare():
            async with deadline:
                await begin_exchange(request, "late", None, None, "rag", "question")

        try:
            with auth.bind_authorization(authorization):
                task = asyncio.create_task(prepare())
                await asyncio.wait_for(entered.wait(), 3)
                if interruption == "timeout":
                    deadline.reschedule(loop.time())
                else:
                    if interruption == "revoke":
                        authorization.revoked.set()
                    task.cancel()
                with pytest.raises(TimeoutError if interruption == "timeout" else asyncio.CancelledError):
                    await task
            release.set()
            assert await asyncio.to_thread(finished.wait, 3)
        finally:
            release.set()

    asyncio.run(run())
    assert authorization not in auth._RUNS.values()
    with sqlite3.connect(temporary_sessions._db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == (1 if stage == "register" else 0)
