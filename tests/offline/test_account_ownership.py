"""Real HTTP scoping and persistent ownership; never use real accounts/models."""

import asyncio
import json
import sqlite3
import threading
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from src.security.request_authorization import (
    AuthorizationRevoked,
    PrivateSessionError,
    RequestAuthorization,
    bind_authorization,
)
from src.security.session_ids import (
    account_scope,
    account_scoped_session_id,
    scoped_session_id,
)
from src.security.user_auth import AuthService, Login, csrf_token

PASSWORD: str = "temporary-password-123"
NEW_PASSWORD: str = "changed-password-456"
ORIGIN: str = "https://workspace.test"


@pytest.fixture
def accounts(
    isolated_runtime: Path, monkeypatch: pytest.MonkeyPatch, temporary_sessions: Any
) -> Any:
    import tiktoken

    monkeypatch.setattr(
        tiktoken,
        "get_encoding",
        lambda _: SimpleNamespace(encode=lambda text: list(text)),
    )
    import app as api
    monkeypatch.setattr(api.app,"middleware_stack",None)
    from config.settings import settings
    from src.agent import memory as memory_module
    from src.agent.memory import MemoryManager, MemoryTurn
    from src.core import generation
    from src.core.agent_runtime import AgentResponse

    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "auth_allowed_origins", (ORIGIN,))
    service: AuthService = AuthService(isolated_runtime / "auth.db")
    monkeypatch.setattr(api.app.state, "user_auth", service, raising=False)
    temporary_sessions.initialize_ownership()
    monkeypatch.setattr(generation, "session_store", temporary_sessions)
    memory = MemoryManager(isolated_runtime / "memory", store=temporary_sessions)
    monkeypatch.setattr(memory_module, "memory_manager", memory)

    class Flow:
        async def run_async(self, shared: dict[str, Any]) -> None:
            temporary_sessions.append_exchange(
                shared["session_id"], shared["query"], "answer"
            )
            shared.update(answer="answer", sources=[])

    class Agent:
        async def execute(self, session_id: str, user_message: str) -> AgentResponse:
            memory.add_turns(session_id, [MemoryTurn("user",user_message),MemoryTurn("assistant","agent answer")])
            return AgentResponse(session_id, "agent answer")

        def reset_session(self, session_id: str) -> bool:
            return memory.clear_session(session_id)

    monkeypatch.setattr(api, "get_online_flow", Flow)
    monkeypatch.setattr(api, "agent_runtime", Agent())
    browsers: dict[str, TestClient] = {}
    logins: dict[str, Login] = {}
    for name in ("alice", "bob"):
        service.create_user(name, PASSWORD, role="member")
        initial = service.login(name, PASSWORD, "local")
        service.change_password(initial.token, PASSWORD, NEW_PASSWORD)
        login: Login = service.login(name, NEW_PASSWORD, "local")
        browser: TestClient = TestClient(api.app, base_url=ORIGIN)
        browser.cookies.set("ragflow_login", login.token)
        browser.headers.update(
            {"Origin": ORIGIN, "X-CSRF-Token": csrf_token(login.token)}
        )
        browsers[name], logins[name] = browser, login
    yield SimpleNamespace(
        api=api,
        service=service,
        store=temporary_sessions,
        memory=memory,
        browsers=browsers,
        logins=logins,
        root=isolated_runtime,
    )
    for browser in browsers.values():
        browser.close()


@contextmanager
def authorize(service: AuthService, login: Login) -> Iterator[RequestAuthorization]:
    authorization: RequestAuthorization = RequestAuthorization(
        login.user.id,
        lambda: service.session_active(login.token),
        lambda: service.guard_session(login.token),
    )
    with bind_authorization(authorization):
        yield authorization


@pytest.mark.parametrize(
    "path,method",
    [
        ("/query", "POST"),
        ("/query/stream?query=hello", "GET"),
        ("/agent/chat", "POST"),
        ("/agent/chat/stream", "POST"),
        ("/session/secret", "GET"),
        ("/session/reset?session_id=secret", "POST"),
        ("/agent/memory/secret", "GET"),
        ("/agent/reset?session_id=secret", "POST"),
        ("/agent/approvals/secret", "GET"),
        ("/agent/approvals/secret/id", "POST"),
        ("/agent/elicitation/secret", "GET"),
        ("/agent/elicitation/secret/id", "POST"),
    ],
)
def test_all_private_legacy_routes_require_account(
    accounts: Any, path: str, method: str
) -> None:
    client: TestClient = accounts.browsers["alice"]
    client.cookies.clear()
    client.cookies.set("ragflow_client", "a" * 32)
    response = client.request(method, path, json={})
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "auth_required"
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("mode", ["rag", "agent"])
def test_same_public_id_different_owners_and_same_account_second_device(
    accounts: Any, mode: str
) -> None:
    path: str = "/query" if mode == "rag" else "/agent/chat"
    history: str = "/session/same-id" if mode == "rag" else "/agent/memory/same-id"
    question_field: str = "query" if mode == "rag" else "message"
    alice: TestClient = accounts.browsers["alice"]
    bob: TestClient = accounts.browsers["bob"]
    assert (
        alice.post(
            path, json={"session_id": "same-id", question_field: "alice private"}
        ).status_code
        == 200
    )
    assert bob.get(history).status_code == 404
    assert (
        bob.post(
            path, json={"session_id": "same-id", question_field: "bob private"}
        ).status_code
        == 200
    )
    assert (
        "alice private" in alice.get(history).text
        and "bob private" not in alice.get(history).text
    )
    assert (
        "bob private" in bob.get(history).text
        and "alice private" not in bob.get(history).text
    )
    second = TestClient(accounts.api.app, base_url=ORIGIN)
    second.cookies.set("ragflow_login", accounts.logins["alice"].token)
    second.cookies.set("ragflow_client", "b" * 32)
    assert "alice private" in second.get(history).text
    assert "bob private" not in second.get(history).text
    reset: str = "/session/reset" if mode == "rag" else "/agent/reset"
    assert bob.post(reset + "?session_id=same-id").status_code == 200
    assert "alice private" in second.get(history).text
    second.close()


def test_storage_owner_checks_and_no_global_list(accounts: Any) -> None:
    alice: Login = accounts.logins["alice"]
    bob: Login = accounts.logins["bob"]
    with authorize(accounts.service, alice):
        alice_id: str = accounts.store.owned_session(
            alice.user.id, "session", "rag", create=True
        )
        accounts.store.append_exchange(alice_id, "private", "answer")
    with authorize(accounts.service, bob):
        for operation in (
            accounts.store.get_history,
            accounts.store.clear,
            accounts.store.read_snapshot,
        ):
            with pytest.raises(PrivateSessionError):
                operation(alice_id)
        with pytest.raises(PrivateSessionError):
            accounts.store.owned_session(alice.user.id, "session", "rag", create=False)
        with pytest.raises(PrivateSessionError):
            accounts.store.list_sessions()


def test_legacy_rows_and_profiles_are_not_claimed(accounts: Any) -> None:
    login: Login = accounts.logins["alice"]
    legacy: str = scoped_session_id("a" * 32, "same-id", "agent")
    accounts.store.append_exchange(legacy, "legacy secret", "old")
    accounts.memory._long_term_path.write_text("global secret", encoding="utf-8")
    with authorize(accounts.service, login):
        owned: str = accounts.store.owned_session(
            login.user.id, "same-id", "agent", create=True
        )
        assert accounts.memory.load_history(owned) == []
        assert accounts.memory.long_term_memory_for_session(owned) == ""
        with pytest.raises(PrivateSessionError):
            accounts.store.get_history(legacy)
    assert accounts.store.get_history(legacy)[0]["content"] == "legacy secret"


def test_long_term_memory_is_account_not_device_scoped(accounts: Any) -> None:
    paths: dict[str, str] = {}
    for name in ("alice", "bob"):
        login: Login = accounts.logins[name]
        path: Path = (
            accounts.root
            / "memory/users"
            / account_scope(login.user.id)
            / "long_term.md"
        )
        path.parent.mkdir(parents=True)
        path.write_text(name + " profile", encoding="utf-8")
        with authorize(accounts.service, login):
            storage: str = accounts.store.owned_session(
                login.user.id, "profile", "agent", create=True
            )
            paths[name] = storage
            assert (
                accounts.memory.long_term_memory_for_session(storage)
                == name + " profile"
            )
    with authorize(accounts.service, accounts.logins["bob"]):
        with pytest.raises(PrivateSessionError):
            accounts.memory.long_term_memory_for_session(paths["alice"])


def test_cache_partitions_and_revocation_before_cache_read(
    accounts: Any, temporary_cache: Any
) -> None:
    from src.llm.cache_context import current_cache_identity, scoped_cache_identity

    messages: list[dict[str, str]] = [{"role": "user", "content": "same"}]
    alice: Login = accounts.logins["alice"]
    with (
        authorize(accounts.service, alice),
        scoped_cache_identity("user\0" + alice.user.id),
    ):
        alice_scope: str = current_cache_identity()
        temporary_cache.set(
            "model", messages, 0.3, "private", identity_scope=alice_scope
        )
        assert (
            temporary_cache.get("model", messages, 0.3, identity_scope=alice_scope)
            == "private"
        )
        accounts.service.logout(alice.token)
        with pytest.raises(AuthorizationRevoked):
            temporary_cache.get("model", messages, 0.3, identity_scope=alice_scope)
    bob: Login = accounts.logins["bob"]
    with (
        authorize(accounts.service, bob),
        scoped_cache_identity("user\0" + bob.user.id),
    ):
        assert current_cache_identity() != alice_scope
        assert (
            temporary_cache.get(
                "model", messages, 0.3, identity_scope=current_cache_identity()
            )
            is None
        )


def test_owned_mapping_is_persistent_and_not_a_hash_only_acl(accounts: Any) -> None:
    user_id: str = accounts.logins["alice"].user.id
    storage: str = accounts.store.owned_session(user_id, "known", "rag", create=True)
    with sqlite3.connect(accounts.store._db_path) as connection:
        assert connection.execute(
            "SELECT owner_user_id,public_id FROM conversations WHERE storage_id=?",
            (storage,),
        ).fetchone() == (user_id, "known")
        connection.execute(
            "UPDATE conversations SET deleted=1 WHERE storage_id=?", (storage,)
        )
    with pytest.raises(PrivateSessionError):
        accounts.store.owned_session(user_id, "known", "rag", create=True)
    assert storage == account_scoped_session_id(user_id, "known", "rag")


def test_same_owner_mode_conflict_and_missing_mode_do_not_alias(accounts: Any) -> None:
    browser: TestClient = accounts.browsers["alice"]
    assert (
        browser.post("/query", json={"session_id": "mode", "query": "one"}).status_code
        == 200
    )
    assert (
        browser.post(
            "/agent/chat", json={"session_id": "mode", "message": "two"}
        ).status_code
        == 409
    )
    assert browser.get("/agent/memory/mode").status_code == 404


def test_stateful_get_sse_requires_csrf_and_post_rejects_foreign_origin(
    accounts: Any,
) -> None:
    browser: TestClient = accounts.browsers["alice"]
    del browser.headers["X-CSRF-Token"]
    assert browser.get("/query/stream?query=hello").status_code == 403
    login: Login = accounts.logins["alice"]
    browser.headers["X-CSRF-Token"] = csrf_token(login.token)
    assert (
        browser.post(
            "/agent/chat",
            json={"session_id": "one", "message": "hello"},
            headers={"Origin": "https://attacker.test"},
        ).status_code
        == 403
    )


@pytest.mark.parametrize("mode", ["rag", "agent"])
def test_complete_real_generation_path_saves_only_the_authenticated_owner(
    accounts: Any, monkeypatch: pytest.MonkeyPatch, fake_llm: Any, mode: str
) -> None:
    alice: TestClient = accounts.browsers["alice"]
    bob: TestClient = accounts.browsers["bob"]
    if mode == "rag":
        from src.core.generation import AnswerInput, answer_service

        class Flow:
            async def run_async(self, shared: dict[str, Any]) -> None:
                entry = AnswerInput(
                    query=shared["query"],
                    context="source",
                    history=[],
                    session_id=shared["session_id"],
                    valid_citation_refs=frozenset(),
                    index_version="legacy",
                )
                answer: str = await answer_service.generate(entry)
                assert answer_service.persist(entry, answer)
                shared.update(answer=answer, sources=[])

        fake_llm.responses.append("real persistence fixture")
        monkeypatch.setattr(accounts.api, "get_online_flow", Flow)
        response = alice.post(
            "/query", json={"session_id": "real", "query": "private user question"}
        )
        history: str = "/session/real"
    else:
        from src.agent.harness import AgentConfig, AgentHarness
        from src.agent.hooks import HookPipeline
        from src.agent.tools import ToolRegistry

        fake_llm.responses.append('{"action":"final_answer"}')
        fake_llm.responses.append("real persistence fixture")
        harness = AgentHarness(
            config=AgentConfig(verbose=False),
            memory=accounts.memory,
            tools=ToolRegistry(),
            hooks=HookPipeline(),
        )
        monkeypatch.setattr(accounts.api, "agent_runtime", harness)
        response = alice.post(
            "/agent/chat",
            json={"session_id": "real", "message": "private user question"},
        )
        history = "/agent/memory/real"
    assert response.status_code == 200
    assert response.json()["answer"] == "real persistence fixture"
    assert "private user question" in alice.get(history).text
    assert bob.get(history).status_code == 404
    turns_path: str = "/conversations/real/turns"
    first_turns = alice.get(turns_path).json()["items"]
    assert len(first_turns) == 1 and first_turns[0]["answer"] == "real persistence fixture"
    assert bob.get(turns_path).status_code == 404
    # Independent credentials on the same account, not a reused browser cookie.
    second_login = accounts.service.login("alice",NEW_PASSWORD,"second-device")
    second = TestClient(accounts.api.app,base_url=ORIGIN)
    try:
        second.cookies.set("ragflow_login",second_login.token)
        second.headers.update({"Origin":ORIGIN,"X-CSRF-Token":csrf_token(second_login.token)})
        assert second.get(turns_path).json()["items"] == first_turns
        if mode == "agent":
            fake_llm.responses.append('{"action":"final_answer"}')
        fake_llm.responses.append("second-device answer")
        turn_id: str = str(uuid.uuid4())
        body = {"session_id":"real","client_turn_id":turn_id,"query" if mode == "rag" else "message":"continue"}
        endpoint: str = "/query" if mode == "rag" else "/agent/chat"
        assert second.post(endpoint,json=body).status_code == 200
        assert len(second.get(turns_path).json()["items"]) == 2
        assert second.post(endpoint,json=body).status_code == 409
        assert second.get(turns_path,params={"client_turn_id":turn_id}).json()["items"][0]["answer"] == "second-device answer"
    finally:
        second.close()


def test_account_member_does_not_gain_shared_mcp_permissions(accounts: Any) -> None:
    from src.agent.tools import SafetyLevel, ToolDef, ToolRegistry, ToolResult

    registry = ToolRegistry(dedup_window=0)
    called: list[bool] = []

    def execute(params: dict[str, Any]) -> ToolResult:
        called.append(True)
        return ToolResult(success=True)

    registry.register(
        ToolDef(
            name="remote",
            description="shared credential fixture",
            params=[],
            safety_level=SafetyLevel.WHITELIST,
            execute_fn=execute,
            source="mcp",
            provider="shared",
            category="external",
        )
    )
    login: Login = accounts.logins["alice"]
    storage: str = accounts.store.owned_session(
        login.user.id, "remote", "agent", create=True
    )
    with authorize(accounts.service, login):
        assert registry.get_model_tools() == []
        result = registry.execute("remote", {}, storage)
        assert not result.success and result.error_code == "tool_policy_denied"
        assert not called


def request_scope(
    login: Login, path: str, method: str = "GET", body: dict[str, Any] | None = None
) -> tuple[dict[str, Any], bytes]:
    route, _, query = path.partition("?")
    data: bytes = json.dumps(body or {}).encode()
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "scheme": "https",
        "method": method,
        "path": route,
        "raw_path": route.encode(),
        "root_path": "",
        "query_string": query.encode(),
        "client": ("127.0.0.1", 1234),
        "server": ("workspace.test", 443),
        "headers": [
            (b"host", b"workspace.test"),
            (b"origin", ORIGIN.encode()),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(data)).encode()),
            (b"cookie", ("ragflow_login=" + login.token).encode()),
            (b"x-csrf-token", csrf_token(login.token).encode()),
        ],
    }
    return scope, data


async def call_app(
    app: Any,
    login: Login,
    path: str,
    method: str,
    body: dict[str, Any] | None,
    messages: list[dict[str, Any]],
    first: asyncio.Event | None = None,
    on_message: Callable[[dict[str, Any]], None] | None = None,
) -> None:
    scope, data = request_scope(login, path, method, body)
    received: bool = False

    async def receive() -> dict[str, Any]:
        nonlocal received
        if not received:
            received = True
            return {"type": "http.request", "body": data, "more_body": False}
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        messages.append(dict(message))
        if on_message is not None:
            on_message(message)
        if first is not None and b"partial" in message.get("body", b""):
            first.set()

    await app(scope, receive, send)


def events(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    text: str = b"".join(message.get("body", b"") for message in messages).decode()
    return [
        json.loads(frame[6:])
        for frame in text.split("\n\n")
        if frame.startswith("data: ")
    ]


@pytest.mark.parametrize("mode", ["rag", "agent"])
@pytest.mark.parametrize(
    "mutation", ["logout", "disable", "reset_password", "expire", "reset"]
)
def test_stream_revocation_closes_reader_and_never_commits(
    accounts: Any, monkeypatch: pytest.MonkeyPatch, mode: str, mutation: str
) -> None:
    from src.core.agent_runtime import AgentEvent, AgentEventKind

    async def exercise() -> None:
        first: asyncio.Event = asyncio.Event()
        closed: asyncio.Event = asyncio.Event()
        saved: list[bool] = []

        class Service:
            async def stream(self, answer_input: Any) -> Any:
                try:
                    yield "partial"
                    await asyncio.Event().wait()
                finally:
                    closed.set()

            def persist(self, answer_input: Any, answer: str) -> bool:
                saved.append(True)
                return True

        class Flow:
            async def run_async(self, shared: dict[str, Any]) -> None:
                shared.update(context="evidence", sources=[])

        class Agent:
            async def events(self, session_id: str, user_message: str) -> Any:
                try:
                    yield AgentEvent(AgentEventKind.CHUNK, chunk="partial")
                    await asyncio.Event().wait()
                    accounts.store.append_exchange(
                        session_id, user_message, "must not save"
                    )
                    saved.append(True)
                finally:
                    closed.set()

            def reset_session(self, session_id: str) -> bool:
                return accounts.memory.clear_session(session_id)

        monkeypatch.setattr(accounts.api, "get_retrieval_flow", Flow)
        monkeypatch.setattr(accounts.api, "answer_service", Service())
        monkeypatch.setattr(accounts.api, "agent_runtime", Agent())
        login: Login = accounts.logins["alice"]
        messages: list[dict[str, Any]] = []
        path: str = (
            "/query/stream?query=hello&session_id=active"
            if mode == "rag"
            else "/agent/chat/stream"
        )
        method: str = "GET" if mode == "rag" else "POST"
        task = asyncio.create_task(
            call_app(
                accounts.api.app,
                login,
                path,
                method,
                {"session_id": "active", "message": "hello"},
                messages,
                first,
            )
        )
        try:
            await asyncio.wait_for(first.wait(), 2)
            if mutation == "logout":
                accounts.service.logout(login.token)
            elif mutation == "disable":
                accounts.service.set_enabled("alice", False)
            elif mutation == "reset_password":
                accounts.service.reset_password("alice", PASSWORD)
            elif mutation == "expire":
                with sqlite3.connect(accounts.root / "auth.db") as connection:
                    connection.execute(
                        "UPDATE auth_sessions SET expires_at=0 WHERE user_id=?",
                        (login.user.id,),
                    )
            else:
                reset_messages: list[dict[str, Any]] = []
                reset: str = "/session/reset" if mode == "rag" else "/agent/reset"
                await call_app(
                    accounts.api.app,
                    login,
                    reset + "?session_id=active",
                    "POST",
                    {},
                    reset_messages,
                )
                assert reset_messages[0]["status"] in {200, 404}
            await asyncio.wait_for(task, 2)
            assert closed.is_set() and not saved
            payloads: list[dict[str, Any]] = events(messages)
            assert sum("error" in payload for payload in payloads) == 1
            assert not any(
                payload.get("done") and "error" not in payload for payload in payloads
            )
            assert not any(
                "must not save" in turn["content"]
                for turn in accounts.store.get_history(
                    account_scoped_session_id(login.user.id, "active", mode)
                )
            )
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "mode,terminal_error",
    [("rag", False), ("agent", False), ("json", False), ("rag", True), ("agent", True)],
)
def test_revocation_during_backpressured_final_send(
    accounts: Any, monkeypatch: pytest.MonkeyPatch, mode: str, terminal_error: bool
) -> None:
    from src.core.agent_runtime import AgentEvent, AgentEventKind, AgentResponse

    async def exercise() -> None:
        login: Login = accounts.logins["alice"]
        blocked: asyncio.Event = asyncio.Event()
        closed: asyncio.Event = asyncio.Event()
        messages: list[dict[str, Any]] = []

        class Service:
            async def stream(self, answer_input: Any) -> Any:
                try:
                    if terminal_error:
                        raise RuntimeError("fixture generation failure")
                    yield "complete"
                finally:
                    closed.set()

            def persist(self, answer_input: Any, answer: str) -> bool:
                return True

        class Flow:
            async def run_async(self, shared: dict[str, Any]) -> None:
                shared.update(context="evidence", sources=[])

        async def completed(session_id: str, message: str) -> Any:
            try:
                yield (
                    AgentEvent(AgentEventKind.ERROR, message="fixture generation failure")
                    if terminal_error else AgentEvent(
                        AgentEventKind.DONE, response=AgentResponse(session_id, "complete")
                    )
                )
            finally:
                closed.set()

        monkeypatch.setattr(accounts.api, "get_retrieval_flow", Flow)
        monkeypatch.setattr(accounts.api, "answer_service", Service())
        monkeypatch.setattr(
            accounts.api.agent_runtime, "events", completed, raising=False
        )
        path: str = (
            "/query/stream?query=hello&session_id=blocked"
            if mode == "rag"
            else "/agent/chat/stream" if mode == "agent" else "/agent/chat"
        )
        scope, data = request_scope(
            login, path, "GET" if mode == "rag" else "POST",
            {"session_id": "blocked", "message": "hello"},
        )
        received: bool = False

        async def receive() -> dict[str, Any]:
            nonlocal received
            if not received:
                received = True
                return {"type": "http.request", "body": data, "more_body": False}
            await asyncio.Event().wait()
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            # Withhold the first terminal only; the connection can still send a
            # subsequent revocation ERROR after cancelling this pending send.
            if not blocked.is_set() and message["type"] == "http.response.body" and (
                mode == "json" or b'"done": true' in message.get("body", b"")
                or b'"error":' in message.get("body", b"")
            ):
                blocked.set()
                await asyncio.Event().wait()
            messages.append(dict(message))

        task = asyncio.create_task(accounts.api.app(scope, receive, send))
        try:
            await asyncio.wait_for(blocked.wait(), 2)
            await asyncio.to_thread(accounts.service.logout, login.token)
            if mode == "json":
                # Once headers are accepted, abort instead of returning normally,
                # replacing the status, or forging an empty Content-Length body.
                with pytest.raises(RuntimeError, match="Private response interrupted"):
                    await asyncio.wait_for(task, 2)
                assert sum(m["type"] == "http.response.start" for m in messages) == 1
                assert not any(m["type"] == "http.response.body" for m in messages)
            else:
                await asyncio.wait_for(task, 2)
                payloads: list[dict[str, Any]] = events(messages)
                terminals: list[dict[str, Any]] = [
                    payload for payload in payloads
                    if payload.get("done") or payload.get("error")
                ]
                assert len(terminals) == 1 and terminals[0].get("error")
                assert not any(payload.get("answer") for payload in payloads)
                assert sum(
                    m["type"] == "http.response.body" and not m.get("more_body", False)
                    for m in messages
                ) == 1
                assert closed.is_set()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(exercise())


@pytest.mark.parametrize("replace", [False, True])
@pytest.mark.parametrize("mutation", ["logout", "disable", "reset_password"])
def test_revoked_credentials_cannot_commit_after_real_sqlite_lock(
    accounts: Any, monkeypatch: pytest.MonkeyPatch, replace: bool, mutation: str
) -> None:
    from src.infra.session_store import SessionTurn

    login: Login = accounts.logins["alice"]
    storage: str = accounts.store.owned_session(
        login.user.id, "locked", "agent", create=True
    )
    accounts.store.append_exchange(storage, "previous", "answer")
    original = accounts.store.read_snapshot(storage)
    attempted: threading.Event = threading.Event()
    connections: list[sqlite3.Connection] = []

    class Connection(sqlite3.Connection):
        def commit(self) -> None:
            if self.total_changes:
                attempted.set()
            super().commit()

    def connect() -> sqlite3.Connection:
        connection = sqlite3.connect(
            accounts.store._db_path,
            timeout=1,
            factory=Connection,
            check_same_thread=False,
        )
        connections.append(connection)
        return connection

    reader = sqlite3.connect(accounts.store._db_path)
    reader.execute("BEGIN")
    reader.execute("SELECT * FROM sessions").fetchall()
    monkeypatch.setattr(accounts.store, "_connect", connect)

    def save() -> bool:
        with authorize(accounts.service, login):
            turns = [SessionTurn("assistant", "late secret")]
            if replace:
                return accounts.store.replace_history_if_revision(
                    storage, original.revision, turns
                )
            return accounts.store.append_turns_if_epoch(
                storage, original.revision[0], turns
            )

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(save)
            try:
                assert attempted.wait(2)
                if mutation == "logout":
                    accounts.service.logout(login.token)
                elif mutation == "disable":
                    accounts.service.set_enabled("alice", False)
                else:
                    accounts.service.reset_password("alice", PASSWORD)
                # Revocation must finish while the history reader STILL holds
                # its lock. An auth read transaction must not span busy retries.
                assert future.result(timeout=1) is False
            finally:
                reader.rollback()
        assert accounts.store.read_snapshot(storage) == original
    finally:
        reader.close()
        for connection in connections:
            connection.close()


@pytest.mark.parametrize("mode", ["form", "url"])
def test_real_elicitation_manager_is_owner_scoped(
    accounts: Any, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    from mcp.types import ElicitRequestFormParams, ElicitRequestURLParams

    from src.agent.mcp_elicitation import MCPElicitationManager

    async def exercise() -> None:
        manager: MCPElicitationManager = MCPElicitationManager(timeout_seconds=2)
        monkeypatch.setattr(
            accounts.api.mcp_runtime_module.mcp_runtime,
            "pending_elicitations",
            manager.pending_for_session,
        )
        monkeypatch.setattr(
            accounts.api.mcp_runtime_module.mcp_runtime,
            "respond_to_elicitation",
            manager.respond,
        )
        ids: dict[str, str] = {
            name: accounts.store.owned_session(
                accounts.logins[name].user.id, "elicit", "agent", create=True
            )
            for name in ("alice", "bob")
        }
        if mode == "form":
            params = ElicitRequestFormParams(
                message="Choose",
                requestedSchema={
                    "type": "object",
                    "properties": {"answer": {"type": "string"}},
                    "required": ["answer"],
                },
            )
            submitted: dict[str, Any] = {
                "action": "accept",
                "content": {"answer": "private"},
            }
        else:
            params = ElicitRequestURLParams(
                message="Open only on click",
                url="https://example.test/authorize",
                elicitationId="server-opaque",
            )
            submitted = {"action": "accept"}
        async with manager.active_call("provider", ids["alice"]):
            pending = asyncio.create_task(
                manager.callback_for("provider")(None, params)
            )
            try:
                await asyncio.sleep(0)
                answers: dict[str, list[dict[str, Any]]] = {"alice": [], "bob": []}
                for name in answers:
                    await call_app(
                        accounts.api.app,
                        accounts.logins[name],
                        "/agent/elicitation/elicit",
                        "GET",
                        None,
                        answers[name],
                    )
                actual: list[dict[str, Any]] = json.loads(answers["alice"][1]["body"])[
                    "pending"
                ]
                assert len(actual) == 1 and actual[0]["mode"] == mode
                assert json.loads(answers["bob"][1]["body"])["pending"] == []
                denied: list[dict[str, Any]] = []
                path: str = "/agent/elicitation/elicit/" + actual[0]["id"]
                await call_app(
                    accounts.api.app,
                    accounts.logins["bob"],
                    path,
                    "POST",
                    submitted,
                    denied,
                )
                assert denied[0]["status"] == 404 and not pending.done()
                approved: list[dict[str, Any]] = []
                await call_app(
                    accounts.api.app,
                    accounts.logins["alice"],
                    path,
                    "POST",
                    submitted,
                    approved,
                )
                assert approved[0]["status"] == 202
                assert (await pending).action == "accept"
            finally:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
        await manager.close()

    asyncio.run(exercise())


def test_logout_after_approval_cannot_start_tool_and_audit_has_safe_actor(
    accounts: Any,
) -> None:
    from src.agent.tool_approval import ToolApprovalManager
    from src.agent.tools import (
        SafetyLevel,
        ToolDef,
        ToolParam,
        ToolRegistry,
        ToolResult,
    )

    async def exercise() -> None:
        manager: ToolApprovalManager = ToolApprovalManager(timeout_seconds=2)
        registry = ToolRegistry(approval_manager=manager, dedup_window=0)
        called: list[bool] = []

        def execute(params: dict[str, Any]) -> ToolResult:
            called.append(True)
            return ToolResult(success=True)

        registry.register(
            ToolDef(
                name="send",
                description="controlled fixture",
                params=[ToolParam("value", "str", "value")],
                safety_level=SafetyLevel.GRAYLIST,
                execute_fn=execute,
                category="test",
            )
        )
        login: Login = accounts.logins["alice"]
        storage: str = accounts.store.owned_session(
            login.user.id, "approved", "agent", create=True
        )
        with authorize(accounts.service, login):
            task = asyncio.create_task(
                registry.execute_async("send", {"value": "private-secret"}, storage)
            )
            try:
                await asyncio.sleep(0)
                pending = await manager.pending_for_session(storage)
                assert len(pending) == 1
                await manager.respond(storage, pending[0]["id"], action="approve")
                accounts.service.logout(login.token)
                result = await task
                assert not result.success and not called
                text: str = (accounts.root / "logs/audit.jsonl").read_text(
                    encoding="utf-8"
                )
                assert "private-secret" not in text and login.token not in text
                assert json.loads(text.splitlines()[-1])[
                    "actor_scope"
                ] == account_scope(login.user.id)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await manager.close()

    asyncio.run(exercise())


def test_same_account_concurrent_generation_conflicts_and_releases_on_reset(
    accounts: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.core.agent_runtime import AgentEvent, AgentEventKind

    async def exercise() -> None:
        first: asyncio.Event = asyncio.Event()

        async def waiting(session_id: str, message: str) -> Any:
            yield AgentEvent(AgentEventKind.CHUNK, chunk="partial")
            await asyncio.Event().wait()

        monkeypatch.setattr(
            accounts.api.agent_runtime, "events", waiting, raising=False
        )
        login: Login = accounts.logins["alice"]
        initial: list[dict[str, Any]] = []
        task = asyncio.create_task(
            call_app(
                accounts.api.app,
                login,
                "/agent/chat/stream",
                "POST",
                {"session_id": "busy", "message": "first"},
                initial,
                first,
            )
        )
        try:
            await asyncio.wait_for(first.wait(), 2)
            conflict: list[dict[str, Any]] = []
            await call_app(
                accounts.api.app,
                login,
                "/agent/chat",
                "POST",
                {"session_id": "busy", "message": "second"},
                conflict,
            )
            assert conflict[0]["status"] == 409
            reset: list[dict[str, Any]] = []
            await call_app(
                accounts.api.app,
                login,
                "/agent/reset?session_id=busy",
                "POST",
                {},
                reset,
            )
            await asyncio.wait_for(task, 2)
            resumed: list[dict[str, Any]] = []
            await call_app(
                accounts.api.app,
                login,
                "/agent/chat",
                "POST",
                {"session_id": "busy", "message": "new"},
                resumed,
            )
            assert resumed[0]["status"] == 200
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(exercise())


def test_real_approval_pending_and_response_are_owner_scoped(
    accounts: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.agent.tool_approval import ToolApprovalManager

    async def exercise() -> None:
        manager: ToolApprovalManager = ToolApprovalManager(timeout_seconds=2)
        monkeypatch.setattr(
            accounts.api.tool_approval_module, "tool_approval_manager", manager
        )
        ids: dict[str, str] = {}
        for name in ("alice", "bob"):
            ids[name] = accounts.store.owned_session(
                accounts.logins[name].user.id, "approval", "agent", create=True
            )
        pending = asyncio.create_task(
            manager.authorize(
                ids["alice"],
                tool_name="demo",
                source="native",
                provider=None,
                category="test",
                params={"value": "private"},
            )
        )
        try:
            await asyncio.sleep(0)
            responses: dict[str, list[dict[str, Any]]] = {"alice": [], "bob": []}
            for name in responses:
                await call_app(
                    accounts.api.app,
                    accounts.logins[name],
                    "/agent/approvals/approval",
                    "GET",
                    None,
                    responses[name],
                )
            alice_body: dict[str, Any] = json.loads(responses["alice"][1]["body"])
            assert len(alice_body["pending"]) == 1
            assert json.loads(responses["bob"][1]["body"])["pending"] == []
            approval_id: str = alice_body["pending"][0]["id"]
            denied: list[dict[str, Any]] = []
            await call_app(
                accounts.api.app,
                accounts.logins["bob"],
                "/agent/approvals/approval/" + approval_id,
                "POST",
                {"action": "approve"},
                denied,
            )
            assert denied[0]["status"] == 404 and not pending.done()
            approved: list[dict[str, Any]] = []
            await call_app(
                accounts.api.app,
                accounts.logins["alice"],
                "/agent/approvals/approval/" + approval_id,
                "POST",
                {"action": "approve"},
                approved,
            )
            assert approved[0]["status"] == 202
            assert (await pending).outcome == "approved"
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            await manager.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("mode", ["rag", "agent"])
def test_revocation_after_done_does_not_emit_a_second_terminal(
    accounts: Any, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    from src.core.agent_runtime import AgentEvent, AgentEventKind, AgentResponse

    async def exercise() -> None:
        closed: asyncio.Event = asyncio.Event()
        login: Login = accounts.logins["alice"]

        class Service:
            async def stream(self, answer_input: Any) -> Any:
                yield "complete"

            def persist(self, answer_input: Any, answer: str) -> bool:
                return True

        class Flow:
            async def run_async(self, shared: dict[str, Any]) -> None:
                shared.update(context="evidence", sources=[])

        async def completed(session_id: str, message: str) -> Any:
            try:
                yield AgentEvent(
                    AgentEventKind.DONE, response=AgentResponse(session_id, "complete")
                )
                await asyncio.Event().wait()
            finally:
                closed.set()

        monkeypatch.setattr(accounts.api, "get_retrieval_flow", Flow)
        monkeypatch.setattr(accounts.api, "answer_service", Service())
        monkeypatch.setattr(
            accounts.api.agent_runtime, "events", completed, raising=False
        )

        def revoke_on_done(message: dict[str, Any]) -> None:
            if b'"done": true' in message.get("body", b""):
                accounts.service.logout(login.token)

        messages: list[dict[str, Any]] = []
        await asyncio.wait_for(
            call_app(
                accounts.api.app,
                login,
                "/query/stream?query=hello&session_id=terminal"
                if mode == "rag"
                else "/agent/chat/stream",
                "GET" if mode == "rag" else "POST",
                {"session_id": "terminal", "message": "hello"},
                messages,
                on_message=revoke_on_done,
            ),
            2,
        )
        payloads: list[dict[str, Any]] = events(messages)
        assert sum(bool(payload.get("done")) for payload in payloads) == 1
        assert not any(payload.get("error") for payload in payloads)
        assert not accounts.service.session_active(login.token)
        if mode == "agent":
            assert closed.is_set()

    asyncio.run(exercise())


def test_real_login_switch_revokes_old_stream_without_inheriting_history(
    accounts: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.core.agent_runtime import AgentEvent, AgentEventKind

    async def exercise() -> None:
        alice: Login = accounts.logins["alice"]
        storage: str = accounts.store.owned_session(
            alice.user.id, "switch", "agent", create=True
        )
        accounts.store.append_exchange(storage, "alice private", "answer")
        first: asyncio.Event = asyncio.Event()
        closed: asyncio.Event = asyncio.Event()

        async def waiting(session_id: str, message: str) -> Any:
            try:
                yield AgentEvent(AgentEventKind.CHUNK, chunk="partial")
                await asyncio.Event().wait()
            finally:
                closed.set()

        monkeypatch.setattr(
            accounts.api.agent_runtime, "events", waiting, raising=False
        )
        messages: list[dict[str, Any]] = []
        task = asyncio.create_task(
            call_app(
                accounts.api.app, alice, "/agent/chat/stream", "POST",
                {"session_id": "switch", "message": "pending"}, messages, first,
            )
        )
        try:
            await asyncio.wait_for(first.wait(), 2)
            browser: TestClient = accounts.browsers["alice"]
            response = await asyncio.to_thread(
                browser.post, "/auth/login",
                json={"username": "bob", "password": NEW_PASSWORD},
            )
            assert response.status_code == 200
            assert response.json()["user"]["id"] == accounts.logins["bob"].user.id
            assert not accounts.service.session_active(alice.token)
            await asyncio.wait_for(task, 2)
            assert closed.is_set()
            assert sum("error" in payload for payload in events(messages)) == 1
            history = await asyncio.to_thread(browser.get, "/agent/memory/switch")
            assert history.status_code == 404 and "alice private" not in history.text
            assert accounts.store.get_history(storage)[0]["content"] == "alice private"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(exercise())


@pytest.mark.parametrize("kind", ["approval", "form", "url"])
def test_logout_closes_real_pending_interaction_and_stream(
    accounts: Any, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    from mcp.types import ElicitRequestFormParams, ElicitRequestURLParams

    from src.agent.mcp_elicitation import MCPElicitationManager
    from src.agent.tool_approval import ToolApprovalManager
    from src.core.agent_runtime import AgentEvent, AgentEventKind

    async def exercise() -> None:
        approval: ToolApprovalManager = ToolApprovalManager(timeout_seconds=5)
        elicitation: MCPElicitationManager = MCPElicitationManager(timeout_seconds=5)
        first: asyncio.Event = asyncio.Event()
        closed: asyncio.Event = asyncio.Event()
        login: Login = accounts.logins["alice"]
        storage: str = accounts.store.owned_session(
            login.user.id, "pending", "agent", create=True
        )

        async def waiting(session_id: str, message: str) -> Any:
            try:
                yield AgentEvent(AgentEventKind.CHUNK, chunk="partial")
                if kind == "approval":
                    await approval.authorize(
                        session_id, tool_name="demo", source="native",
                        provider=None, category="test", params={},
                    )
                else:
                    params = (
                        ElicitRequestFormParams(
                            message="Choose", requestedSchema={
                                "type": "object", "properties": {},
                            },
                        )
                        if kind == "form"
                        else ElicitRequestURLParams(
                            message="Open on click", url="https://example.test/auth",
                            elicitationId="opaque",
                        )
                    )
                    async with elicitation.active_call("fixture", session_id):
                        await elicitation.callback_for("fixture")(None, params)
            finally:
                closed.set()

        monkeypatch.setattr(
            accounts.api.agent_runtime, "events", waiting, raising=False
        )
        messages: list[dict[str, Any]] = []
        task = asyncio.create_task(
            call_app(
                accounts.api.app, login, "/agent/chat/stream", "POST",
                {"session_id": "pending", "message": "hello"}, messages, first,
            )
        )
        manager = approval if kind == "approval" else elicitation
        try:
            await asyncio.wait_for(first.wait(), 2)

            async def wait_pending() -> None:
                while not await manager.pending_for_session(storage):
                    await asyncio.sleep(0)

            await asyncio.wait_for(wait_pending(), 2)
            accounts.service.logout(login.token)
            await asyncio.wait_for(task, 2)
            assert closed.is_set()
            assert not await manager.pending_for_session(storage)
            assert sum("error" in payload for payload in events(messages)) == 1
            assert accounts.store.get_history(storage) == []
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await approval.close()
            await elicitation.close()

    asyncio.run(exercise())
