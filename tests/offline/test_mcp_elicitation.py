"""MCP Elicitation reaches the owning Agent session through public seams."""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from contextvars import Context as ContextState
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any, Literal, cast

import pytest
from fastapi.testclient import TestClient
from mcp import Client
from mcp.client.session import ClientRequestContext
from mcp.server.mcpserver import AcceptedElicitation, Context, MCPServer
from mcp.server.mcpserver.resolve import Elicit, Resolve
from mcp.types import (
    ElicitRequestFormParams,
    ElicitRequestURLParams,
    ErrorData,
)
from pydantic import BaseModel


class _ModernFormResponse(BaseModel):
    answer: str


def _request_modern_answer() -> Elicit[_ModernFormResponse]:
    return Elicit("Choose a modern answer", _ModernFormResponse)


class _CopyGuardBodyChunk:
    """Fail if the application copies a chunk known to exceed its budget."""

    def __init__(self, size: int) -> None:
        self._size = size

    def __len__(self) -> int:
        return self._size

    def __bool__(self) -> bool:
        return True

    def __iter__(self) -> object:
        raise AssertionError("Oversized ASGI body chunk was copied")


async def _asgi_post_chunks(
    application: Any,
    path: str,
    chunks: tuple[bytes | _CopyGuardBodyChunk, ...],
) -> tuple[int, dict[str, object]]:
    """Issue one streaming POST without a Content-Length header."""
    next_chunk = 0
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        nonlocal next_chunk
        if next_chunk >= len(chunks):
            return {"type": "http.disconnect"}
        chunk = chunks[next_chunk]
        next_chunk += 1
        return {
            "type": "http.request",
            "body": chunk,
            "more_body": next_chunk < len(chunks),
        }

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await application(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
            "root_path": "",
        },
        receive,
        send,
    )
    status = next(
        message["status"]
        for message in messages
        if message["type"] == "http.response.start"
    )
    response_body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    return status, json.loads(response_body)


def test_form_elicitation_reaches_session_and_returns_acceptance() -> None:
    from config.settings import MCPStdioServerConfig
    from src.agent.mcp_client import MCPClientManager
    from src.agent.tools import ToolRegistry

    class FormResponse(BaseModel):
        answer: str

    server = MCPServer("elicitation-form-test")

    @server.tool(name="ask")
    async def ask(ctx: Context) -> dict[str, str]:
        outcome = await ctx.elicit("Choose an answer", FormResponse)
        if isinstance(outcome, AcceptedElicitation):
            return {"action": "accept", "answer": outcome.data.answer}
        return {"action": outcome.action}

    configured = MCPStdioServerConfig(
        id="interactive",
        transport="stdio",
        command="unused-in-unit-tests",
    )
    registry = ToolRegistry(dedup_window=0)
    manager: MCPClientManager

    def create_client(config: MCPStdioServerConfig) -> Client:
        return Client(
            server,
            mode="legacy",
            elicitation_callback=manager.elicitation_callback(config.id),
        )

    manager = MCPClientManager([configured], client_factory=create_client)

    async def exercise() -> tuple[tuple[dict[str, object], ...], object]:
        await manager.start(registry)
        call = asyncio.create_task(
            registry.execute_async(
                "mcp__interactive__ask",
                {},
                session_id="scoped-agent-session",
            )
        )
        for _ in range(100):
            pending = await manager.pending_elicitations(
                "scoped-agent-session"
            )
            if pending:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("Elicitation did not reach the caller session")

        await manager.respond_to_elicitation(
            "scoped-agent-session",
            pending[0]["id"],
            action="accept",
            content={"answer": "approved"},
        )
        result = await call
        await manager.close()
        return pending, result

    pending, result = asyncio.run(exercise())

    assert pending == (
        {
            "id": pending[0]["id"],
            "server_id": "interactive",
            "mode": "form",
            "message": "Choose an answer",
            "requested_schema": {
                "properties": {"answer": {"title": "Answer", "type": "string"}},
                "required": ["answer"],
                "title": "FormResponse",
                "type": "object",
            },
        },
    )
    assert result.success is True
    assert result.data == {"action": "accept", "answer": "approved"}


def test_modern_input_required_elicitation_uses_the_same_session_flow() -> None:
    from config.settings import MCPStdioServerConfig
    from src.agent.mcp_client import MCPClientManager
    from src.agent.tools import ToolRegistry

    server = MCPServer("elicitation-modern-test")

    @server.tool(name="ask_modern")
    async def ask_modern(
        response: Annotated[
            _ModernFormResponse,
            Resolve(_request_modern_answer),
        ],
    ) -> dict[str, str]:
        return {"answer": response.answer}

    configured = MCPStdioServerConfig(
        id="modern",
        transport="stdio",
        command="unused-in-unit-tests",
    )
    registry = ToolRegistry(dedup_window=0)
    manager: MCPClientManager

    def create_client(config: MCPStdioServerConfig) -> Client:
        return Client(
            server,
            elicitation_callback=manager.elicitation_callback(config.id),
        )

    manager = MCPClientManager([configured], client_factory=create_client)

    async def exercise() -> tuple[tuple[dict[str, object], ...], object]:
        await manager.start(registry)
        call = asyncio.create_task(
            registry.execute_async(
                "mcp__modern__ask_modern",
                {},
                session_id="modern-session",
            )
        )
        for _ in range(100):
            pending = await manager.pending_elicitations("modern-session")
            if pending:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("Modern Elicitation did not reach the session")
        await manager.respond_to_elicitation(
            "modern-session",
            pending[0]["id"],
            action="accept",
            content={"answer": "modern-approved"},
        )
        result = await call
        await manager.close()
        return pending, result

    pending, result = asyncio.run(exercise())

    assert pending[0]["mode"] == "form"
    assert pending[0]["message"] == "Choose a modern answer"
    assert result.success is True
    assert result.data == {"answer": "modern-approved"}


def test_default_stdio_factory_injects_elicitation_callback() -> None:
    from config.settings import MCPStdioServerConfig
    from src.agent.mcp_client import MCPClientManager
    from src.agent.tools import ToolRegistry

    project_root = Path(__file__).resolve().parents[2]
    server = MCPStdioServerConfig(
        id="stdio_elicitation",
        transport="stdio",
        command=sys.executable,
        args=(str(project_root / "tests" / "fixtures" / "mcp_stdio_server.py"),),
        cwd=str(project_root),
        call_timeout_seconds=5,
    )
    registry = ToolRegistry(dedup_window=0)
    manager = MCPClientManager([server])

    async def exercise() -> tuple[tuple[dict[str, object], ...], object]:
        await manager.start(registry)
        try:
            call = asyncio.create_task(
                registry.execute_async(
                    "mcp__stdio_elicitation__ask",
                    {},
                    session_id="stdio-session",
                )
            )
            for _ in range(200):
                pending = await manager.pending_elicitations("stdio-session")
                if pending:
                    break
                await asyncio.sleep(0.01)
            else:
                raise AssertionError("stdio Elicitation did not reach the session")
            await manager.respond_to_elicitation(
                "stdio-session",
                pending[0]["id"],
                action="accept",
                content={"answer": "stdio-approved"},
            )
            result = await call
            return pending, result
        finally:
            await manager.close()

    pending, result = asyncio.run(exercise())

    assert pending[0]["message"] == "Choose a stdio answer"
    assert result.success is True
    assert result.data == {"answer": "stdio-approved"}


def test_agent_session_can_query_and_respond_without_content_echo(
    monkeypatch: pytest.MonkeyPatch,
    isolated_runtime: Path,
) -> None:
    del isolated_runtime
    import tiktoken

    monkeypatch.setattr(
        tiktoken,
        "get_encoding",
        lambda name: SimpleNamespace(encode=lambda text: list(text)),
    )
    import app as api

    calls: list[tuple[object, ...]] = []

    class FakeRuntime:
        async def pending_elicitations(
            self,
            session_id: str,
        ) -> tuple[dict[str, object], ...]:
            calls.append(("pending", session_id))
            return (
                {
                    "id": "request-1",
                    "server_id": "interactive",
                    "mode": "form",
                    "message": "Choose an answer",
                    "requested_schema": {
                        "type": "object",
                        "properties": {"answer": {"type": "string"}},
                        "required": ["answer"],
                    },
                },
            )

        async def respond_to_elicitation(
            self,
            session_id: str,
            elicitation_id: str,
            *,
            action: str,
            content: dict[str, object] | None,
        ) -> None:
            calls.append(
                ("respond", session_id, elicitation_id, action, content)
            )

    monkeypatch.setattr(
        api.mcp_runtime_module,
        "mcp_runtime",
        FakeRuntime(),
    )
    client = TestClient(api.app)
    try:
        pending = client.get("/agent/elicitation/chat-session")
        response = client.post(
            "/agent/elicitation/chat-session/request-1",
            json={"action": "accept", "content": {"answer": "secret"}},
        )
    finally:
        client.close()

    assert pending.status_code == 200
    assert pending.headers["cache-control"] == "no-store"
    assert pending.json() == {
        "session_id": "chat-session",
        "pending": [
            {
                "id": "request-1",
                "server_id": "interactive",
                "mode": "form",
                "message": "Choose an answer",
                "requested_schema": {
                    "type": "object",
                    "properties": {"answer": {"type": "string"}},
                    "required": ["answer"],
                },
            }
        ],
    }
    assert response.status_code == 202
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"status": "received"}
    assert "secret" not in response.text
    assert calls[0][0] == "pending"
    assert calls[1][0] == "respond"
    assert calls[0][1] == calls[1][1]
    assert calls[0][1] != "chat-session"


def test_elicitation_response_endpoint_rejects_oversized_request_body(
    monkeypatch: pytest.MonkeyPatch,
    isolated_runtime: Path,
) -> None:
    del isolated_runtime
    import tiktoken

    monkeypatch.setattr(
        tiktoken,
        "get_encoding",
        lambda name: SimpleNamespace(encode=lambda text: list(text)),
    )
    import app as api

    called = False

    class FakeRuntime:
        async def respond_to_elicitation(
            self,
            session_id: str,
            elicitation_id: str,
            *,
            action: str,
            content: dict[str, object] | None,
        ) -> None:
            nonlocal called
            del session_id, elicitation_id, action, content
            called = True

    monkeypatch.setattr(
        api.mcp_runtime_module,
        "mcp_runtime",
        FakeRuntime(),
    )
    client = TestClient(api.app)
    try:
        response = client.post(
            "/agent/elicitation/chat-session/request-1",
            json={"action": "accept", "content": {"answer": "x" * 1_000_000}},
        )
    finally:
        client.close()

    assert response.status_code == 413
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["detail"]["code"] == (
        "mcp_elicitation_response_too_large"
    )
    assert "xxx" not in response.text
    assert called is False


def test_elicitation_response_rejects_oversized_asgi_chunk_before_copy(
    monkeypatch: pytest.MonkeyPatch,
    isolated_runtime: Path,
) -> None:
    del isolated_runtime
    import tiktoken

    monkeypatch.setattr(
        tiktoken,
        "get_encoding",
        lambda name: SimpleNamespace(encode=lambda text: list(text)),
    )
    import app as api

    called = False

    class FakeRuntime:
        async def respond_to_elicitation(self, *args: object, **kwargs: object) -> None:
            nonlocal called
            del args, kwargs
            called = True

    monkeypatch.setattr(api.mcp_runtime_module, "mcp_runtime", FakeRuntime())
    status, payload = asyncio.run(
        _asgi_post_chunks(
            api.app,
            "/agent/elicitation/chat-session/request-1",
            (
                _CopyGuardBodyChunk(
                    api.MAX_ELICITATION_RESPONSE_BODY_BYTES + 1
                ),
            ),
        )
    )

    assert status == 413
    assert payload["detail"]["code"] == "mcp_elicitation_response_too_large"
    assert called is False


def test_elicitation_response_rejects_cumulative_chunks_before_overflow_copy(
    monkeypatch: pytest.MonkeyPatch,
    isolated_runtime: Path,
) -> None:
    del isolated_runtime
    import tiktoken

    monkeypatch.setattr(
        tiktoken,
        "get_encoding",
        lambda name: SimpleNamespace(encode=lambda text: list(text)),
    )
    import app as api

    called = False

    class FakeRuntime:
        async def respond_to_elicitation(self, *args: object, **kwargs: object) -> None:
            nonlocal called
            del args, kwargs
            called = True

    monkeypatch.setattr(api.mcp_runtime_module, "mcp_runtime", FakeRuntime())
    status, payload = asyncio.run(
        _asgi_post_chunks(
            api.app,
            "/agent/elicitation/chat-session/request-1",
            (
                b"x" * api.MAX_ELICITATION_RESPONSE_BODY_BYTES,
                _CopyGuardBodyChunk(1),
            ),
        )
    )

    assert status == 413
    assert payload["detail"]["code"] == "mcp_elicitation_response_too_large"
    assert called is False


def test_invalid_form_response_is_rejected_while_request_remains_pending() -> None:
    from src.agent.mcp_elicitation import (
        MCPElicitationInvalidResponse,
        MCPElicitationManager,
    )

    coordinator = MCPElicitationManager(timeout_seconds=1)
    callback = coordinator.callback_for("interactive")
    params = ElicitRequestFormParams(
        message="Choose an answer",
        requestedSchema={
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        },
    )

    async def exercise() -> tuple[object, tuple[dict[str, object], ...]]:
        async with coordinator.active_call("interactive", "session-a"):
            request = asyncio.create_task(
                callback(cast(ClientRequestContext, None), params)
            )
            await asyncio.sleep(0)
            pending = await coordinator.pending_for_session("session-a")
            with pytest.raises(MCPElicitationInvalidResponse):
                await coordinator.respond(
                    "session-a",
                    pending[0]["id"],
                    action="accept",
                    content={"answer": 42},
                )
            still_pending = await coordinator.pending_for_session("session-a")
            assert request.done() is False
            await coordinator.respond(
                "session-a",
                pending[0]["id"],
                action="accept",
                content={"answer": "valid"},
            )
            result = await request
        return result, still_pending

    result, still_pending = asyncio.run(exercise())

    assert result.action == "accept"
    assert result.content == {"answer": "valid"}
    assert len(still_pending) == 1


def test_host_rejects_oversized_form_content_without_releasing_request() -> None:
    from src.agent.mcp_elicitation import (
        MCPElicitationInvalidResponse,
        MCPElicitationManager,
    )

    coordinator = MCPElicitationManager(timeout_seconds=1)
    callback = coordinator.callback_for("interactive")
    params = ElicitRequestFormParams(
        message="Choose an answer",
        requestedSchema={
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
        },
    )

    async def exercise() -> tuple[object, tuple[dict[str, object], ...]]:
        async with coordinator.active_call("interactive", "session-a"):
            request = asyncio.create_task(
                callback(cast(ClientRequestContext, None), params)
            )
            await asyncio.sleep(0)
            pending = await coordinator.pending_for_session("session-a")
            with pytest.raises(MCPElicitationInvalidResponse):
                await coordinator.respond(
                    "session-a",
                    pending[0]["id"],
                    action="accept",
                    content={"answer": "x" * 1_000_000},
                )
            still_pending = await coordinator.pending_for_session("session-a")
            await coordinator.respond(
                "session-a",
                pending[0]["id"],
                action="cancel",
                content=None,
            )
            result = await request
        return result, still_pending

    result, still_pending = asyncio.run(exercise())

    assert result.action == "cancel"
    assert len(still_pending) == 1


def test_response_validation_does_not_block_other_sessions() -> None:
    from src.agent.mcp_elicitation import MCPElicitationManager

    validator_entered = threading.Event()
    release_validator = threading.Event()

    def blocking_validator(
        schema: dict[str, object],
        content: object,
    ) -> str | None:
        del schema, content
        validator_entered.set()
        if not release_validator.wait(timeout=2):
            return "validator timed out"
        return None

    coordinator = MCPElicitationManager(
        timeout_seconds=2,
        response_validator=blocking_validator,
    )
    callback_a = coordinator.callback_for("server-a")
    callback_b = coordinator.callback_for("server-b")
    params = ElicitRequestFormParams(
        message="Choose an answer",
        requestedSchema={
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
        },
    )

    async def exercise() -> tuple[float, object, object]:
        async with coordinator.active_call("server-a", "session-a"):
            request_a = asyncio.create_task(
                callback_a(cast(ClientRequestContext, None), params)
            )
            await asyncio.sleep(0)
            pending_a = await coordinator.pending_for_session("session-a")
            async with coordinator.active_call("server-b", "session-b"):
                request_b = asyncio.create_task(
                    callback_b(cast(ClientRequestContext, None), params)
                )
                await asyncio.sleep(0)
                pending_b = await coordinator.pending_for_session("session-b")

                response = asyncio.create_task(
                    coordinator.respond(
                        "session-a",
                        pending_a[0]["id"],
                        action="accept",
                        content={"answer": "valid"},
                    )
                )
                for _ in range(1_000):
                    if validator_entered.is_set():
                        break
                    await asyncio.sleep(0.001)
                else:
                    raise AssertionError("Response validation did not start")
                started_at = asyncio.get_running_loop().time()
                visible = await asyncio.wait_for(
                    coordinator.pending_for_session("session-b"),
                    timeout=0.2,
                )
                elapsed = asyncio.get_running_loop().time() - started_at
                assert visible == pending_b
                release_validator.set()
                await response
                await coordinator.respond(
                    "session-b",
                    pending_b[0]["id"],
                    action="cancel",
                    content=None,
                )
                return elapsed, await request_a, await request_b

    elapsed, result_a, result_b = asyncio.run(exercise())

    assert validator_entered.is_set()
    assert elapsed < 0.2
    assert result_a.action == "accept"
    assert result_b.action == "cancel"


@pytest.mark.parametrize("action", ["decline", "cancel"])
def test_non_accept_form_response_returns_without_content(
    action: Literal["decline", "cancel"],
) -> None:
    from src.agent.mcp_elicitation import MCPElicitationManager

    coordinator = MCPElicitationManager(timeout_seconds=1)
    callback = coordinator.callback_for("interactive")
    params = ElicitRequestFormParams(
        message="Choose an answer",
        requestedSchema={"type": "object", "properties": {}},
    )

    async def exercise() -> object:
        async with coordinator.active_call("interactive", "session-a"):
            request = asyncio.create_task(
                callback(cast(ClientRequestContext, None), params)
            )
            await asyncio.sleep(0)
            pending = await coordinator.pending_for_session("session-a")
            await coordinator.respond(
                "session-a",
                pending[0]["id"],
                action=action,
                content=None,
            )
            return await request

    result = asyncio.run(exercise())

    assert result.action == action
    assert result.content is None


def test_elicitation_timeout_returns_cancel_and_cleans_pending() -> None:
    from src.agent.mcp_elicitation import MCPElicitationManager

    coordinator = MCPElicitationManager(timeout_seconds=0.01)
    callback = coordinator.callback_for("interactive")
    params = ElicitRequestFormParams(
        message="Choose an answer",
        requestedSchema={"type": "object", "properties": {}},
    )

    async def exercise() -> tuple[object, tuple[dict[str, object], ...]]:
        async with coordinator.active_call("interactive", "session-a"):
            result = await callback(cast(ClientRequestContext, None), params)
            pending = await coordinator.pending_for_session("session-a")
        return result, pending

    result, pending = asyncio.run(exercise())

    assert result.action == "cancel"
    assert result.content is None
    assert pending == ()


def test_url_elicitation_projects_https_and_rejects_public_http() -> None:
    from src.agent.mcp_elicitation import MCPElicitationManager

    coordinator = MCPElicitationManager(timeout_seconds=1)
    callback = coordinator.callback_for("interactive")
    secure = ElicitRequestURLParams(
        message="Complete sign-in",
        url="https://login.example.test/authorize",
        elicitationId="opaque-server-id",
    )
    insecure = ElicitRequestURLParams(
        message="Do not expose",
        url="http://example.com/secret",
        elicitationId="unsafe-server-id",
    )

    async def exercise() -> tuple[tuple[dict[str, object], ...], object]:
        async with coordinator.active_call("interactive", "session-a"):
            request = asyncio.create_task(
                callback(cast(ClientRequestContext, None), secure)
            )
            await asyncio.sleep(0)
            pending = await coordinator.pending_for_session("session-a")
            await coordinator.respond(
                "session-a",
                pending[0]["id"],
                action="accept",
                content=None,
            )
            assert (await request).action == "accept"
        async with coordinator.active_call("interactive", "session-a"):
            rejected = await asyncio.wait_for(
                callback(cast(ClientRequestContext, None), insecure),
                timeout=0.1,
            )
        return pending, rejected

    pending, rejected = asyncio.run(exercise())

    assert pending == (
        {
            "id": pending[0]["id"],
            "server_id": "interactive",
            "mode": "url",
            "message": "Complete sign-in",
            "url": "https://login.example.test/authorize",
            "elicitation_id": "opaque-server-id",
        },
    )
    assert isinstance(rejected, ErrorData)
    assert rejected.message == "Elicitation request was rejected"


def test_other_client_cannot_read_or_respond_to_pending_elicitation(
    monkeypatch: pytest.MonkeyPatch,
    isolated_runtime: Path,
) -> None:
    del isolated_runtime
    import tiktoken

    monkeypatch.setattr(
        tiktoken,
        "get_encoding",
        lambda name: SimpleNamespace(encode=lambda text: list(text)),
    )
    import app as api
    from src.agent.mcp_elicitation import MCPElicitationNotPending

    owner: str | None = None

    class FakeRuntime:
        async def pending_elicitations(
            self,
            session_id: str,
        ) -> tuple[dict[str, object], ...]:
            nonlocal owner
            if owner is None:
                owner = session_id
            if session_id != owner:
                return ()
            return ({"id": "request-1", "mode": "form"},)

        async def respond_to_elicitation(
            self,
            session_id: str,
            elicitation_id: str,
            *,
            action: str,
            content: dict[str, object] | None,
        ) -> None:
            del elicitation_id, action, content
            if session_id != owner:
                raise MCPElicitationNotPending()

    monkeypatch.setattr(
        api.mcp_runtime_module,
        "mcp_runtime",
        FakeRuntime(),
    )
    client_a = TestClient(api.app)
    client_b = TestClient(api.app)
    try:
        own = client_a.get("/agent/elicitation/chat-session")
        foreign = client_b.get("/agent/elicitation/chat-session")
        rejected = client_b.post(
            "/agent/elicitation/chat-session/request-1",
            json={"action": "cancel"},
        )
    finally:
        client_a.close()
        client_b.close()

    assert own.json()["pending"] == [{"id": "request-1", "mode": "form"}]
    assert foreign.json()["pending"] == []
    assert rejected.status_code == 404
    assert rejected.headers["cache-control"] == "no-store"
    assert rejected.json()["detail"]["code"] == "mcp_elicitation_not_pending"


def test_ambiguous_legacy_callback_is_rejected_without_cross_session_leak() -> None:
    from src.agent.mcp_elicitation import MCPElicitationManager

    coordinator = MCPElicitationManager(timeout_seconds=1)
    callback = coordinator.callback_for("interactive")
    params = ElicitRequestFormParams(
        message="Choose an answer",
        requestedSchema={"type": "object", "properties": {}},
    )

    async def exercise() -> tuple[object, tuple[dict[str, object], ...]]:
        ready: asyncio.Queue[None] = asyncio.Queue()
        release = asyncio.Event()

        async def hold_call(session_id: str) -> None:
            async with coordinator.active_call("interactive", session_id):
                ready.put_nowait(None)
                await release.wait()

        first = asyncio.create_task(hold_call("session-a"))
        second = asyncio.create_task(hold_call("session-b"))
        await ready.get()
        await ready.get()
        request = asyncio.create_task(
            callback(cast(ClientRequestContext, None), params),
            context=ContextState(),
        )
        result = await request
        pending_a = await coordinator.pending_for_session("session-a")
        pending_b = await coordinator.pending_for_session("session-b")
        release.set()
        await asyncio.gather(first, second)
        return result, pending_a + pending_b

    result, pending = asyncio.run(exercise())

    assert isinstance(result, ErrorData)
    assert result.message == (
        "Elicitation cannot be associated with an active tool call"
    )
    assert pending == ()


def test_manager_shutdown_cancels_and_cleans_pending_elicitation() -> None:
    from src.agent.mcp_elicitation import MCPElicitationManager

    coordinator = MCPElicitationManager(timeout_seconds=1)
    callback = coordinator.callback_for("interactive")
    params = ElicitRequestFormParams(
        message="Choose an answer",
        requestedSchema={"type": "object", "properties": {}},
    )

    async def exercise() -> tuple[object, tuple[dict[str, object], ...]]:
        async with coordinator.active_call("interactive", "session-a"):
            request = asyncio.create_task(
                callback(cast(ClientRequestContext, None), params)
            )
            await asyncio.sleep(0)
            assert await coordinator.pending_for_session("session-a")
            await coordinator.close()
            result = await request
            pending = await coordinator.pending_for_session("session-a")
        return result, pending

    result, pending = asyncio.run(exercise())

    assert result.action == "cancel"
    assert pending == ()


def test_closed_manager_rejects_new_elicitation_without_pending_state() -> None:
    from src.agent.mcp_elicitation import MCPElicitationManager

    coordinator = MCPElicitationManager(timeout_seconds=1)
    callback = coordinator.callback_for("interactive")
    params = ElicitRequestFormParams(
        message="Choose an answer",
        requestedSchema={"type": "object", "properties": {}},
    )

    async def exercise() -> tuple[object, tuple[dict[str, object], ...]]:
        await coordinator.close()
        async with coordinator.active_call("interactive", "session-a"):
            result = await asyncio.wait_for(
                callback(cast(ClientRequestContext, None), params),
                timeout=0.1,
            )
            pending = await coordinator.pending_for_session("session-a")
        return result, pending

    result, pending = asyncio.run(exercise())

    assert isinstance(result, ErrorData)
    assert result.message == "Elicitation manager is closed"
    assert pending == ()


def test_timeout_wins_atomically_against_response_validation() -> None:
    from src.agent.mcp_elicitation import (
        MCPElicitationManager,
        MCPElicitationNotPending,
    )

    validator_entered = threading.Event()
    release_validator = threading.Event()

    def blocking_validator(
        schema: dict[str, object],
        content: object,
    ) -> str | None:
        del schema, content
        validator_entered.set()
        release_validator.wait(timeout=2)
        return None

    coordinator = MCPElicitationManager(
        timeout_seconds=0.2,
        response_validator=blocking_validator,
    )
    callback = coordinator.callback_for("interactive")
    params = ElicitRequestFormParams(
        message="Choose an answer",
        requestedSchema={
            "type": "object",
            "properties": {"answer": {"type": "string"}},
        },
    )

    async def exercise() -> tuple[object, tuple[dict[str, object], ...]]:
        async with coordinator.active_call("interactive", "session-a"):
            request = asyncio.create_task(
                callback(cast(ClientRequestContext, None), params)
            )
            await asyncio.sleep(0)
            pending = await coordinator.pending_for_session("session-a")
            response = asyncio.create_task(
                coordinator.respond(
                    "session-a",
                    pending[0]["id"],
                    action="accept",
                    content={"answer": "valid"},
                )
            )
            for _ in range(1_000):
                if validator_entered.is_set():
                    break
                await asyncio.sleep(0.001)
            else:
                raise AssertionError("Response validation did not start")
            result = await request
            after_timeout = await coordinator.pending_for_session("session-a")
            release_validator.set()
            with pytest.raises(MCPElicitationNotPending):
                await response
            return result, after_timeout

    try:
        result, pending = asyncio.run(exercise())
    finally:
        release_validator.set()

    assert result.action == "cancel"
    assert pending == ()


def test_close_wins_atomically_against_response_validation() -> None:
    from src.agent.mcp_elicitation import (
        MCPElicitationManager,
        MCPElicitationNotPending,
    )

    validator_entered = threading.Event()
    release_validator = threading.Event()

    def blocking_validator(
        schema: dict[str, object],
        content: object,
    ) -> str | None:
        del schema, content
        validator_entered.set()
        release_validator.wait(timeout=2)
        return None

    coordinator = MCPElicitationManager(
        timeout_seconds=2,
        response_validator=blocking_validator,
    )
    callback = coordinator.callback_for("interactive")
    params = ElicitRequestFormParams(
        message="Choose an answer",
        requestedSchema={
            "type": "object",
            "properties": {"answer": {"type": "string"}},
        },
    )

    async def exercise() -> tuple[object, tuple[dict[str, object], ...]]:
        async with coordinator.active_call("interactive", "session-a"):
            request = asyncio.create_task(
                callback(cast(ClientRequestContext, None), params)
            )
            await asyncio.sleep(0)
            pending = await coordinator.pending_for_session("session-a")
            response = asyncio.create_task(
                coordinator.respond(
                    "session-a",
                    pending[0]["id"],
                    action="accept",
                    content={"answer": "valid"},
                )
            )
            for _ in range(100):
                if validator_entered.is_set():
                    break
                await asyncio.sleep(0.001)
            else:
                raise AssertionError("Response validation did not start")
            await coordinator.close()
            result = await request
            after_close = await coordinator.pending_for_session("session-a")
            release_validator.set()
            with pytest.raises(MCPElicitationNotPending):
                await response
            return result, after_close

    try:
        result, pending = asyncio.run(exercise())
    finally:
        release_validator.set()

    assert result.action == "cancel"
    assert pending == ()
