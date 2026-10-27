"""Account history contracts using real SQLite and synthetic authenticated HTTP."""

import asyncio
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from contextvars import copy_context
from pathlib import Path
from typing import Any

import pytest
from starlette.requests import ClientDisconnect

from src.infra.exchange_context import ExchangeContext, exchange_context
from src.infra.session_store import CommitPermit, SessionTurn
from tests.offline.test_account_ownership import accounts as accounts
from tests.offline.test_account_ownership import authorize, call_app


def test_offline_sqlite_guard_rejects_an_outside_path_before_creating_it(isolated_runtime: Any) -> None:
    outside = isolated_runtime.parent / (isolated_runtime.name + "-forbidden.db")
    assert not outside.exists()
    with pytest.raises(pytest.fail.Exception,match="outside isolated runtime"):
        sqlite3.connect(outside)
    assert not outside.exists()


@pytest.mark.parametrize("form", ["absolute", "relative", "encoded", "authority", "bytes"])
@pytest.mark.parametrize("uri_mode", ["ro", "rwc"])
def test_offline_sqlite_guard_rejects_file_uris_before_connecting(
    isolated_runtime: Path, form: str, uri_mode: str
) -> None:
    outside: Path = isolated_runtime.parent / (isolated_runtime.name + "-uri-forbidden.db")
    assert not outside.exists()
    uri: str = "file:" + outside.as_posix() + "?mode=" + uri_mode
    if form == "relative":
        uri = "file:../" + outside.name + "?mode=" + uri_mode
    elif form == "encoded":
        uri = outside.as_uri() + "?mode=" + uri_mode
    elif form == "authority":
        uri = outside.as_uri().replace("file:///", "file://localhost/") + "?mode=" + uri_mode
    target: str | bytes = uri.encode() if form == "bytes" else uri
    with pytest.raises(pytest.fail.Exception, match="outside isolated runtime"):
        with closing(sqlite3.connect(target, uri=True)) as connection:
            connection.execute("CREATE TABLE synthetic_guard_probe(value INTEGER)")
    assert not outside.exists()


def test_offline_sqlite_guard_allows_normal_paths_and_memory(isolated_runtime: Path) -> None:
    for target in (isolated_runtime / "allowed.db", ":memory:"):
        with closing(sqlite3.connect(target)) as connection:
            assert connection.execute("SELECT 1").fetchone() == (1,)


@pytest.mark.parametrize("form", ["absolute", "encoded", "authority", "bytes"])
def test_offline_sqlite_guard_preserves_local_readonly_uri_semantics(
    isolated_runtime: Path, form: str
) -> None:
    database: Path = isolated_runtime / ("encoded # 中文 %.db" if form == "encoded" else "readonly.db")
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("CREATE TABLE synthetic(value INTEGER)")
        connection.commit()
    uri: str = database.as_uri() + "?mode=ro" if form == "encoded" else "file:" + database.as_posix() + "?mode=ro"
    if form == "authority":
        uri = database.as_uri().replace("file:///", "file://localhost/") + "?mode=ro"
    target: str | bytes = uri.encode() if form == "bytes" else uri
    with closing(sqlite3.connect(target, uri=True)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM synthetic").fetchone() == (0,)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("INSERT INTO synthetic VALUES(1)")


def test_source_snapshot_is_bounded_and_never_copies_paths_or_provider_metadata() -> (
    None
):
    from src.infra.exchange_context import safe_sources

    projection = safe_sources(
        [
            {
                "ref": 1,
                "source": "https://provider.test/auth?token=private-secret",
                "metadata": {"private": "raw remote result"},
                "text": "excerpt" * 100,
                "document_id": "logical-id",
                "position": {"page": 2},
            }
        ]
        * 60
    )
    assert len(projection) == 50 and len(projection[0]["excerpt"]) == 360
    assert projection[0]["page"] == 2
    assert "private-secret" not in str(projection) and "raw remote result" not in str(
        projection
    )


def create(accounts: Any, mode: str = "rag", owner: str = "alice") -> dict[str, Any]:
    response = accounts.browsers[owner].post(
        "/conversations", json={"id": str(uuid.uuid4()), "mode": mode}
    )
    assert response.status_code == 201, response.text
    return response.json()


def append(
    accounts: Any,
    conversation: dict[str, Any],
    *,
    question: str = "private question",
    owner: str = "alice",
    turn_id: str | None = None,
    used_model: str | None = None,
) -> str:
    turn_id = turn_id or str(uuid.uuid4())
    login = accounts.logins[owner]
    with authorize(accounts.service, login):
        storage = accounts.store.owned_session(
            login.user.id, conversation["id"], conversation["mode"], create=False
        )
        token = exchange_context.set(ExchangeContext(turn_id, question, used_model=used_model))
        try:
            assert accounts.store.append_turns_if_epoch(
                storage,
                conversation["epoch"],
                [
                    SessionTurn("user", question),
                    SessionTurn("assistant", "complete answer"),
                ],
            )
        finally:
            exchange_context.reset(token)
    return turn_id


@pytest.mark.parametrize("mode", ["rag", "agent"])
def test_latest_model_is_atomic_metadata_preserved_by_refusal_and_clear(accounts: Any, mode: str) -> None:
    item = create(accounts, mode)
    browser = accounts.browsers["alice"]
    path: str = "/conversations/" + item["id"]
    assert item["last_model"] is None
    append(accounts, item, used_model="fixture-answer-model")
    assert browser.get(path).json()["last_model"] == "fixture-answer-model"
    assert browser.get("/conversations", params={"mode": mode}).json()["items"][0]["last_model"] == "fixture-answer-model"
    assert browser.get(path + "/turns").json()["items"][0]["used_model"] == "fixture-answer-model"
    append(accounts, item)  # A deterministic refusal makes no model call.
    assert browser.get(path).json()["last_model"] == "fixture-answer-model"
    assert browser.get(path + "/turns").json()["items"][-1]["used_model"] is None
    append(accounts, item, used_model="fixture-next-model")
    summary = browser.get(path).json()
    assert summary["last_model"] == "fixture-next-model"
    cleared = browser.post(path + "/clear", json={"expected_revision": summary["revision"]})
    assert cleared.status_code == 200
    assert cleared.json()["last_model"] == "fixture-next-model"
    assert browser.get(path + "/turns").json()["items"] == []


@pytest.mark.parametrize("replace", [False, True])
def test_latest_model_rolls_back_with_the_completed_exchange(accounts: Any, replace: bool) -> None:
    item = create(accounts)
    append(accounts, item, used_model="fixture-previous-model")
    login = accounts.logins["alice"]
    with authorize(accounts.service, login):
        storage = accounts.store.owned_session(login.user.id, item["id"], "rag", create=False)
        snapshot = accounts.store.read_snapshot(storage)
        original = accounts.store.conversation(item["id"])
        token = exchange_context.set(ExchangeContext(str(uuid.uuid4()), "cancelled", used_model="fixture-cancelled-model"))
        try:
            turns: list[SessionTurn] = [SessionTurn("user", "cancelled"), SessionTurn("assistant", "answer")]
            if replace:
                assert not accounts.store.replace_history_if_revision(storage, snapshot.revision, turns, can_commit=lambda: False)
            else:
                assert not accounts.store.append_turns_if_epoch(storage, item["epoch"], turns, can_commit=lambda: False)
        finally:
            exchange_context.reset(token)
        assert accounts.store.conversation(item["id"]) == original
        assert accounts.store.conversation(item["id"])["last_model"] == "fixture-previous-model"
        assert len(accounts.store.transcript(item["id"])["items"]) == 1
        assert accounts.store.read_snapshot(storage).turns == snapshot.turns


@pytest.mark.parametrize("mode", ["rag", "agent"])
def test_directory_lifecycle_and_owner(accounts: Any, mode: str) -> None:
    item = create(accounts, mode)
    alice, bob = accounts.browsers["alice"], accounts.browsers["bob"]
    path = "/conversations/" + item["id"]
    assert (
        alice.post("/conversations", json={"id": item["id"], "mode": mode}).json()
        == item
    )
    assert (
        alice.post(
            "/conversations",
            json={"id": item["id"], "mode": "agent" if mode == "rag" else "rag"},
        ).status_code
        == 409
    )
    assert bob.get(path).status_code == 404
    assert bob.get(path + "/turns").status_code == 404
    assert (
        bob.patch(path, json={"title": "steal", "expected_revision": 0}).status_code
        == 404
    )
    assert bob.delete(path, headers={"If-Match": "0"}).status_code == 404
    assert bob.get("/conversations").json()["items"] == []
    turn_id = append(accounts, item)
    saved = alice.get(path).json()
    assert saved["title"] == "private question" and saved["revision"] == 1
    assert (
        alice.get(path + "/turns", params={"client_turn_id": turn_id}).json()["items"][
            0
        ]["client_turn_id"]
        == turn_id
    )
    assert create(accounts, mode)["id"] != item["id"]
    assert len(alice.get(path + "/turns").json()["items"]) == 1  # new preserves old
    renamed = alice.patch(path, json={"title": " renamed ", "expected_revision": 1})
    assert renamed.status_code == 200 and renamed.json()["title"] == "renamed"
    assert alice.post(path + "/clear", json={"expected_revision": 1}).status_code == 409
    cleared = alice.post(path + "/clear", json={"expected_revision": 2}).json()
    assert cleared["epoch"] == 1 and cleared["title"] == "renamed"
    assert alice.get(path + "/turns").json()["items"] == []
    assert (
        alice.delete(path, headers={"If-Match": str(cleared["revision"])}).status_code
        == 204
    )
    assert alice.delete(path, headers={"If-Match": "0"}).status_code == 204
    assert alice.get(path).status_code == 404
    assert (
        alice.post("/conversations", json={"id": item["id"], "mode": mode}).status_code
        == 404
    )


def test_pagination_exact_match_and_safe_search(accounts: Any) -> None:
    item = create(accounts)
    browser = accounts.browsers["alice"]
    ids = [
        append(accounts, item, question="question " + str(index)) for index in range(23)
    ]
    path = "/conversations/" + item["id"] + "/turns"
    latest = browser.get(path).json()
    assert len(latest["items"]) == 20 and latest["items"][0]["seq"] == 4
    older = browser.get(path, params={"before": latest["next_before"]}).json()
    assert [turn["seq"] for turn in older["items"]] == [1, 2, 3]
    assert (
        browser.get(path, params={"client_turn_id": ids[0]}).json()["items"][0]["seq"]
        == 1
    )
    assert (
        browser.get(path, params={"client_turn_id": str(uuid.uuid4())}).json()["items"]
        == []
    )
    for params in (
        {"client_turn_id": ids[0], "limit": 20},
        {"client_turn_id": ids[0], "before": "bad"},
        {"client_turn_id": "bad"},
        {"before": "bad"},
    ):
        assert browser.get(path, params=params).status_code == 422
    other = create(accounts)
    assert (
        browser.get(
            "/conversations/" + other["id"] + "/turns",
            params={"before": latest["next_before"]},
        ).status_code
        == 422
    )
    page = browser.get("/conversations", params={"limit": 1}).json()
    next_page = browser.get(
        "/conversations", params={"limit": 1, "cursor": page["next_cursor"]}
    ).json()
    assert page["items"][0]["id"] != next_page["items"][0]["id"]
    assert (
        browser.post(
            "/conversations/search", json={"title_query": "question 0"}
        ).json()["items"][0]["id"]
        == item["id"]
    )


@pytest.mark.parametrize("replace", [False, True])
def test_atomic_memory_transcript_directory_rollback(
    accounts: Any, replace: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = create(accounts)
    login = accounts.logins["alice"]
    with authorize(accounts.service, login):
        storage = accounts.store.owned_session(
            login.user.id, item["id"], "rag", create=False
        )
        snapshot = accounts.store.read_snapshot(storage)
        original = accounts.store._commit_completed_exchange

        def fail_after_transcript(connection: Any, session_id: str, turns: Any) -> None:
            original(connection, session_id, turns)
            raise sqlite3.OperationalError("synthetic failure")

        monkeypatch.setattr(
            accounts.store, "_commit_completed_exchange", fail_after_transcript
        )
        token = exchange_context.set(ExchangeContext(str(uuid.uuid4()), "private"))
        try:
            with pytest.raises(sqlite3.OperationalError):
                turns = [
                    SessionTurn("user", "private"),
                    SessionTurn("assistant", "answer"),
                ]
                if replace:
                    accounts.store.replace_history_if_revision(
                        storage, snapshot.revision, turns
                    )
                else:
                    accounts.store.append_turns_if_epoch(storage, 0, turns)
        finally:
            exchange_context.reset(token)
        assert accounts.store.get_history(storage) == []
        assert accounts.store.transcript(item["id"])["items"] == []
        assert accounts.store.conversation(item["id"])["revision"] == 0


def test_compression_preserves_complete_transcript(
    accounts: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = create(accounts, "agent")
    accounts.memory.config.compress_trigger_turns = 4
    accounts.memory.config.max_history_turns = 2
    monkeypatch.setattr(accounts.memory, "_llm_compress", lambda _: "synthetic summary")
    login = accounts.logins["alice"]
    from src.agent.memory import MemoryTurn

    with authorize(accounts.service, login):
        storage = accounts.store.owned_session(
            login.user.id, item["id"], "agent", create=False
        )
        for index in range(4):
            token = exchange_context.set(
                ExchangeContext(str(uuid.uuid4()), f"question {index}")
            )
            try:
                accounts.memory.add_turns(
                    storage,
                    [
                        MemoryTurn("user", f"question {index}"),
                        MemoryTurn("assistant", "answer"),
                    ],
                )
            finally:
                exchange_context.reset(token)
        assert len(accounts.store.transcript(item["id"])["items"]) == 4
        assert len(accounts.store.get_turns(storage)) < 8


def test_cancelled_commit_rolls_back_transcript_too(accounts: Any) -> None:
    item = create(accounts)
    login = accounts.logins["alice"]
    with authorize(accounts.service, login):
        storage = accounts.store.owned_session(
            login.user.id, item["id"], "rag", create=False
        )
        permit = CommitPermit()
        permit.revoke()
        token = exchange_context.set(ExchangeContext(str(uuid.uuid4()), "private"))
        try:
            assert not accounts.store.append_turns_if_epoch(
                storage,
                0,
                [SessionTurn("user", "private"), SessionTurn("assistant", "answer")],
                can_commit=permit,
            )
        finally:
            exchange_context.reset(token)
        assert not accounts.store.transcript(item["id"])["items"]
        assert not accounts.store.get_history(storage)


def test_duplicate_turn_conflict_before_flow_and_mismatched_ids(accounts: Any) -> None:
    item = create(accounts)
    turn_id = append(accounts, item)
    browser = accounts.browsers["alice"]
    assert (
        browser.post(
            "/query",
            json={
                "conversation_id": item["id"],
                "client_turn_id": turn_id,
                "query": "do not rerun",
            },
        ).status_code
        == 409
    )


@pytest.mark.parametrize("replace", [False, True])
def test_busy_commit_cancellation_rolls_back_all_three_views(
    accounts: Any, monkeypatch: pytest.MonkeyPatch, replace: bool
) -> None:
    item = create(accounts)
    login = accounts.logins["alice"]
    blocked = threading.Event()

    class Connection(sqlite3.Connection):
        def commit(self) -> None:
            try:
                super().commit()
            except sqlite3.OperationalError as exc:
                if getattr(exc, "sqlite_errorcode", 0) & 0xFF == sqlite3.SQLITE_BUSY:
                    blocked.set()
                raise

    def connect() -> sqlite3.Connection:
        return sqlite3.connect(accounts.store._db_path, timeout=30, factory=Connection)

    with authorize(accounts.service, login):
        storage = accounts.store.owned_session(
            login.user.id, item["id"], "rag", create=False
        )
        snapshot = accounts.store.read_snapshot(storage)
        token = exchange_context.set(ExchangeContext(str(uuid.uuid4()), "private"))
        reader = sqlite3.connect(accounts.store._db_path)
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM sessions").fetchall()
        permit = CommitPermit()
        monkeypatch.setattr(accounts.store, "_connect", connect)

        def write() -> bool:
            pending = [
                SessionTurn("user", "private"),
                SessionTurn("assistant", "answer"),
            ]
            if replace:
                return accounts.store.replace_history_if_revision(
                    storage, snapshot.revision, pending, can_commit=permit
                )
            return accounts.store.append_turns_if_epoch(
                storage, 0, pending, can_commit=permit
            )

        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(copy_context().run, write)
                try:
                    assert blocked.wait(3), "never reached real busy COMMIT"
                    permit.revoke()
                finally:
                    reader.rollback()
                assert future.result(timeout=3) is False
            assert accounts.store.get_history(storage) == []
            assert accounts.store.transcript(item["id"])["items"] == []
            assert accounts.store.conversation(item["id"])["revision"] == 0
        finally:
            reader.close()
            exchange_context.reset(token)


@pytest.mark.parametrize("path", ["/ready", "/agent/tools", "/conversations"])
def test_new_private_reads_require_login(accounts: Any, path: str) -> None:
    browser = accounts.browsers["alice"]
    browser.cookies.clear()
    response = browser.get(path)
    assert (
        response.status_code == 401 and response.headers["cache-control"] == "no-store"
    )


@pytest.mark.parametrize(
    "path", ["/docs", "/redoc", "/openapi.json", "/index/jobs/known"]
)
def test_management_reads_still_require_admin_key_not_user_role(
    accounts: Any, path: str
) -> None:
    browser = accounts.browsers["alice"]
    assert browser.get(path).status_code == 401
    assert browser.get("/health").json() == {"status": "ok"}
    assert browser.get("/workspace/config").json() == {"auth_enabled": True}


def test_model_reservations_persist_and_do_not_leak_to_another_owner(
    accounts: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from config.settings import settings
    from src.infra.session_store import SessionStore
    from src.security.request_authorization import AccountLimitExceeded

    monkeypatch.setattr(settings, "account_daily_model_units", 100)
    monkeypatch.setattr(settings, "source_daily_model_units", 1000)
    with authorize(accounts.service, accounts.logins["alice"]):
        accounts.store.reserve_model_units(60)
        reopened = SessionStore(accounts.store._db_path)
        with pytest.raises(AccountLimitExceeded):
            reopened.reserve_model_units(41)
        reopened.reserve_model_units(40)
    with authorize(accounts.service, accounts.logins["bob"]):
        accounts.store.reserve_model_units(100)
        with pytest.raises(AccountLimitExceeded):
            accounts.store.reserve_model_units(1)


def test_model_reservation_source_cap_is_atomic(
    accounts: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from config.settings import settings
    from src.security.request_authorization import AccountLimitExceeded

    monkeypatch.setattr(settings, "account_daily_model_units", 100)
    monkeypatch.setattr(settings, "source_daily_model_units", 100)
    with authorize(accounts.service, accounts.logins["alice"]):
        accounts.store.reserve_model_units(60)
    with authorize(accounts.service, accounts.logins["bob"]):
        with pytest.raises(AccountLimitExceeded):
            accounts.store.reserve_model_units(41)
        accounts.store.reserve_model_units(
            40
        )  # prior failed reservation did not spend bob's units


def test_account_rate_and_concurrency_are_released_without_quota_refund(
    accounts: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from config.settings import settings
    from src.security.account_limits import AccountRequestLimits

    monkeypatch.setattr(settings, "account_requests_per_minute", 2)
    monkeypatch.setattr(settings, "source_requests_per_minute", 3)
    monkeypatch.setattr(settings, "account_max_concurrent", 1)
    monkeypatch.setattr(settings, "source_max_concurrent", 2)
    limits = AccountRequestLimits()
    assert limits.acquire("alice", "source")
    assert not limits.acquire("alice", "other")
    assert limits.acquire("bob", "source")
    assert not limits.acquire("charlie", "source")
    limits.release("alice", "source")
    limits.release("bob", "source")
    assert limits.acquire("alice", "source")
    limits.release("alice", "source")
    assert not limits.acquire("alice", "source")
    assert not limits.acquire("charlie", "source")


def test_validation_does_not_echo_private_title(accounts: Any) -> None:
    item = create(accounts)
    response = accounts.browsers["alice"].patch(
        "/conversations/" + item["id"],
        json={
            "expected_revision": 0,
            "title": "private secret",
            "unknown": "private secret",
        },
    )
    assert response.status_code == 422 and "private secret" not in response.text


def test_mismatched_conversation_and_session_ids(accounts: Any) -> None:
    item = create(accounts)
    browser = accounts.browsers["alice"]
    assert (
        browser.post(
            "/query",
            json={
                "conversation_id": item["id"],
                "session_id": "different",
                "query": "private",
            },
        ).status_code
        == 422
    )


@pytest.mark.parametrize("path", ["sync", "async", "native", "stream"])
def test_each_llm_entry_reserves_before_any_provider_or_cache(
    accounts: Any, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    from src.llm import llm_client
    from src.security.request_authorization import AccountLimitExceeded

    def exhausted(units: int) -> None:
        raise AccountLimitExceeded("account_quota_exceeded")

    monkeypatch.setattr(accounts.store, "reserve_model_units", exhausted)

    async def call() -> None:
        if path == "async":
            await llm_client.chat_async([{"role": "user", "content": "synthetic"}])
        elif path == "native":
            await llm_client.chat_with_tools_async(
                [{"role": "user", "content": "synthetic"}], []
            )
        else:
            async for _ in llm_client.chat_stream_async(
                [{"role": "user", "content": "synthetic"}]
            ):
                pytest.fail("unexpected provider output")

    with (
        authorize(accounts.service, accounts.logins["alice"]),
        pytest.raises(AccountLimitExceeded),
    ):
        if path == "sync":
            llm_client.chat([{"role": "user", "content": "synthetic"}])
        else:
            asyncio.run(call())


def test_member_allowlist_never_bypasses_registry_policy(
    accounts: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from config.settings import settings
    from src.agent.tool_policy import ToolAccessPolicy
    from src.agent.tools import SafetyLevel, ToolDef, ToolRegistry, ToolResult

    monkeypatch.setattr(settings, "member_mcp_allowlist", ("remote",))
    called: list[bool] = []

    def run(params: dict[str, Any]) -> ToolResult:
        called.append(True)
        return ToolResult(success=True)

    tool = ToolDef(
        name="remote",
        description="synthetic",
        params=[],
        safety_level=SafetyLevel.WHITELIST,
        execute_fn=run,
        source="mcp",
        provider="shared",
        category="read",
    )
    login = accounts.logins["alice"]
    storage = accounts.store.owned_session(
        login.user.id, "remote", "agent", create=True
    )
    with authorize(accounts.service, login):
        denied = ToolRegistry(policy=ToolAccessPolicy(denylist=("remote",)))
        denied.register(tool)
        assert denied.execute("remote", {}, storage).error_code == "tool_policy_denied"
        assert not called
        allowed = ToolRegistry()
        allowed.register(tool)
        assert allowed.execute("remote", {}, storage).success
        assert called == [True]


@pytest.mark.parametrize("mode", ["rag", "agent"])
def test_lost_done_preserves_durable_exchange_and_exact_lookup(
    accounts: Any, monkeypatch: pytest.MonkeyPatch, fake_llm: Any, mode: str
) -> None:
    from src.agent.harness import AgentConfig, AgentHarness
    from src.agent.hooks import HookPipeline
    from src.agent.tools import ToolRegistry

    item = create(accounts, mode)
    turn_id: str = str(uuid.uuid4())
    messages: list[dict[str, Any]] = []

    class Retrieval:
        async def run_async(self, shared: dict[str, Any]) -> None:
            shared.update(
                context="synthetic evidence",
                sources=[{"ref": 1, "text": "safe excerpt", "source": "private-path"}],
                index_version="fixture",
                session_epoch=accounts.store.read_snapshot(
                    shared["session_id"]
                ).revision[0],
            )

    if mode == "rag":
        monkeypatch.setattr(accounts.api, "get_retrieval_flow", Retrieval)
    else:
        fake_llm.responses.append('{"action":"final_answer"}')
        monkeypatch.setattr(
            accounts.api,
            "agent_runtime",
            AgentHarness(
                config=AgentConfig(verbose=False),
                memory=accounts.memory,
                tools=ToolRegistry(),
                hooks=HookPipeline(),
            ),
        )
    fake_llm.responses.append("durable answer")

    def lose_final_send(message: dict[str, Any]) -> None:
        if b'"done": true' in message.get("body", b""):
            messages.pop()  # transport did not accept this frame
            raise ConnectionResetError("synthetic disconnected transport")

    async def run() -> None:
        # StreamingResponse maps failed ASGI sends to ClientDisconnect.
        with pytest.raises(ClientDisconnect):
            await call_app(
                accounts.api.app,
                accounts.logins["alice"],
                f"/query/stream?query=synthetic&conversation_id={item['id']}&client_turn_id={turn_id}"
                if mode == "rag"
                else "/agent/chat/stream",
                "GET" if mode == "rag" else "POST",
                None
                if mode == "rag"
                else {
                    "conversation_id": item["id"],
                    "client_turn_id": turn_id,
                    "message": "synthetic",
                },
                messages,
                on_message=lose_final_send,
            )

    asyncio.run(run())
    assert not any(b'"done": true' in message.get("body", b"") for message in messages)
    browser = accounts.browsers["alice"]
    record = browser.get(
        "/conversations/" + item["id"] + "/turns", params={"client_turn_id": turn_id}
    ).json()
    assert (
        len(record["items"]) == 1 and record["items"][0]["answer"] == "durable answer"
    )
    assert "private-path" not in str(record)


def test_invalid_turn_id_is_rejected_before_stateless_flow(
    accounts: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def never_start() -> Any:
        pytest.fail("invalid ID reached generation")

    monkeypatch.setattr(accounts.api, "get_online_flow", never_start)
    assert (
        accounts.browsers["alice"]
        .post("/query", json={"query": "synthetic", "client_turn_id": "bad"})
        .status_code
        == 422
    )


def test_cursor_out_of_range_number_is_stable_validation_not_overflow(
    accounts: Any,
) -> None:
    import hashlib

    resource: str = "conversations:rag:" + hashlib.sha256(b"").hexdigest()[:16]
    cursor: str = accounts.store._cursor([resource, 10**309, "safe-id"])
    response = accounts.browsers["alice"].get(
        "/conversations", params={"mode": "rag", "cursor": cursor}
    )
    assert response.status_code == 422
    item = create(accounts)
    before: str = accounts.store._cursor(["turns", item["id"], 10**100])
    assert accounts.browsers["alice"].get(
        "/conversations/" + item["id"] + "/turns", params={"before": before}
    ).status_code == 422
