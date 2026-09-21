"""Standard MCP OAuth client integration stays explicit and secret-free."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from types import ModuleType, SimpleNamespace

import httpx2
import pytest
from fastapi.testclient import TestClient
from mcp.client.auth import OAuthClientProvider, OAuthTokenError
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import SecretStr, ValidationError


def _oauth_server(**oauth_overrides: object):
    from config.settings import MCPStreamableHTTPServerConfig

    oauth = {
        "redirect_uri": (
            "http://127.0.0.1:8000/agent/oauth/remote/callback"
        ),
        "client_name": "ragrag tests",
        "scope": "mcp:tools offline_access",
    }
    oauth.update(oauth_overrides)
    return MCPStreamableHTTPServerConfig(
        id="remote",
        transport="streamable_http",
        url="https://mcp.example.test/mcp",
        oauth=oauth,
    )


def _import_app(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    import tiktoken

    monkeypatch.setattr(
        tiktoken,
        "get_encoding",
        lambda name: SimpleNamespace(encode=lambda text: list(text)),
    )
    import app

    return app


def test_oauth_configuration_is_optional_and_validated() -> None:
    server = _oauth_server(
        client_metadata_url="https://client.example.test/metadata.json",
    )

    assert server.oauth is not None
    assert str(server.oauth.redirect_uri) == (
        "http://127.0.0.1:8000/agent/oauth/remote/callback"
    )
    assert server.oauth.application_type == "native"
    assert str(server.oauth.client_metadata_url) == (
        "https://client.example.test/metadata.json"
    )

    from config.settings import MCPStreamableHTTPServerConfig

    plain = MCPStreamableHTTPServerConfig(
        id="plain",
        transport="streamable_http",
        url="https://mcp.example.test/mcp",
    )
    assert plain.oauth is None


@pytest.mark.parametrize(
    "redirect_uri",
    [
        "http://127.0.0.1:8000/agent/oauth/remote/callback",
        "http://[::1]:8000/agent/oauth/remote/callback",
        "https://client.example.test/agent/oauth/remote/callback",
    ],
)
def test_oauth_redirect_uri_allows_https_or_native_loopback_http(
    redirect_uri: str,
) -> None:
    server = _oauth_server(redirect_uri=redirect_uri)

    assert server.oauth is not None
    assert str(server.oauth.redirect_uri) == redirect_uri


@pytest.mark.parametrize(
    "oauth",
    [
        {"redirect_uri": "https://user:secret@example.test/callback"},
        {"redirect_uri": "https://example.test/callback#fragment"},
        {"redirect_uri": "http://example.test/callback"},
        {"redirect_uri": "http://192.168.1.10/callback"},
        {
            "redirect_uri": "http://127.0.0.1:8000/callback",
            "application_type": "web",
        },
        {
            "redirect_uri": "https://example.test/callback",
            "client_name": "   ",
        },
        {
            "redirect_uri": "https://example.test/callback",
            "client_metadata_url": "http://example.test/client.json",
        },
        {
            "redirect_uri": "https://example.test/callback",
            "client_metadata_url": "https://example.test/",
        },
    ],
)
def test_oauth_configuration_rejects_unsafe_values(
    oauth: dict[str, object],
) -> None:
    from config.settings import MCPStreamableHTTPServerConfig

    with pytest.raises(ValidationError):
        MCPStreamableHTTPServerConfig(
            id="remote",
            transport="streamable_http",
            url="https://mcp.example.test/mcp",
            oauth=oauth,
        )


def test_in_memory_storage_copies_tokens_and_client_information() -> None:
    from src.agent.mcp_oauth import InMemoryOAuthTokenStorage

    storage = InMemoryOAuthTokenStorage()
    tokens = OAuthToken(
        access_token="access-secret",
        refresh_token="refresh-secret",
        expires_in=60,
    )
    client_info = OAuthClientInformationFull(
        client_id="client-id",
        redirect_uris=[
            "http://127.0.0.1:8000/agent/oauth/remote/callback"
        ],
        token_endpoint_auth_method="none",
    )

    async def exercise() -> None:
        await storage.set_tokens(tokens)
        await storage.set_client_info(client_info)
        loaded_tokens = await storage.get_tokens()
        loaded_info = await storage.get_client_info()
        assert loaded_tokens == tokens
        assert loaded_tokens is not tokens
        assert loaded_info == client_info
        assert loaded_info is not client_info

    asyncio.run(exercise())


def test_oauth_flow_uses_sdk_provider_and_validates_callback_state() -> None:
    from src.agent.mcp_oauth import (
        MCPOAuthCallbackStateMismatch,
        MCPOAuthFlowManager,
    )

    manager = MCPOAuthFlowManager((_oauth_server(),))
    provider = manager.provider_for(_oauth_server())
    assert isinstance(provider, OAuthClientProvider)

    async def exercise() -> None:
        redirect = provider.context.redirect_handler
        callback = provider.context.callback_handler
        assert redirect is not None
        assert callback is not None
        authorization_url = (
            "https://login.example.test/authorize"
            "?client_id=public-client&state=expected-state"
        )
        await redirect(authorization_url)
        snapshot = await manager.status("remote")
        assert snapshot == {
            "server_id": "remote",
            "status": "awaiting_callback",
            "authorization_url": authorization_url,
            "error_code": None,
        }

        waiter = asyncio.create_task(callback())
        with pytest.raises(MCPOAuthCallbackStateMismatch):
            await manager.complete_callback(
                "remote",
                code="authorization-code-secret",
                state="wrong-state",
                issuer=None,
            )
        assert not waiter.done()

        await manager.complete_callback(
            "remote",
            code="authorization-code-secret",
            state="expected-state",
            issuer="https://login.example.test",
        )
        result = await waiter
        assert result.code == "authorization-code-secret"
        assert result.state == "expected-state"
        assert result.iss == "https://login.example.test"
        assert "authorization-code-secret" not in repr(
            await manager.status("remote")
        )

    asyncio.run(exercise())


def test_pending_oauth_authorization_can_be_cancelled() -> None:
    from src.agent.mcp_oauth import (
        MCPOAuthAuthorizationCancelled,
        MCPOAuthFlowManager,
    )

    manager = MCPOAuthFlowManager((_oauth_server(),))
    provider = manager.provider_for(_oauth_server())

    async def exercise() -> None:
        redirect = provider.context.redirect_handler
        callback = provider.context.callback_handler
        assert redirect is not None
        assert callback is not None
        await redirect(
            "https://login.example.test/authorize?state=cancel-state"
        )
        waiter = asyncio.create_task(callback())
        await manager.cancel_authorization("remote")
        with pytest.raises(MCPOAuthAuthorizationCancelled):
            await waiter
        assert await manager.status("remote") == {
            "server_id": "remote",
            "status": "cancelled",
            "authorization_url": None,
            "error_code": "mcp_oauth_cancelled",
        }

    asyncio.run(exercise())


def test_streamable_http_injects_official_oauth_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.agent import mcp_client

    captured: dict[str, object] = {}

    class FakeHTTPClient:
        async def __aenter__(self) -> FakeHTTPClient:
            return self

        async def __aexit__(
            self,
            exc_type: object,
            exc_value: object,
            traceback: object,
        ) -> None:
            return None

    class FakeMCPClient:
        def __init__(self, transport: object) -> None:
            captured["transport"] = transport

        async def __aenter__(self) -> FakeMCPClient:
            return self

        async def __aexit__(
            self,
            exc_type: object,
            exc_value: object,
            traceback: object,
        ) -> None:
            return None

    def create_http_client(**options: object) -> FakeHTTPClient:
        captured.update(options)
        return FakeHTTPClient()

    def create_transport(
        url: str,
        *,
        http_client: object,
    ) -> object:
        captured["url"] = url
        captured["http_client"] = http_client
        return object()

    monkeypatch.setattr(mcp_client.httpx2, "AsyncClient", create_http_client)
    monkeypatch.setattr(mcp_client, "streamable_http_client", create_transport)
    monkeypatch.setattr(mcp_client, "Client", FakeMCPClient)

    async def exercise() -> None:
        async with mcp_client._default_client_factory(_oauth_server()):
            pass

    asyncio.run(exercise())

    assert isinstance(captured["auth"], OAuthClientProvider)
    assert str(captured["url"]) == "https://mcp.example.test/mcp"
    assert getattr(captured["timeout"], "connect") == 10


def test_official_provider_refreshes_through_replaceable_storage() -> None:
    from src.agent.mcp_oauth import InMemoryOAuthTokenStorage, MCPOAuthFlowManager

    storage = InMemoryOAuthTokenStorage()
    manager = MCPOAuthFlowManager(
        (_oauth_server(),),
        storage_factory=lambda server_id: storage,
    )
    provider = manager.provider_for(_oauth_server())
    old_tokens = OAuthToken(
        access_token="old-access-secret",
        refresh_token="refresh-secret",
        expires_in=1,
    )
    client_info = OAuthClientInformationFull(
        client_id="client-id",
        redirect_uris=[
            "http://127.0.0.1:8000/agent/oauth/remote/callback"
        ],
        token_endpoint_auth_method="none",
    )
    resource_authorization: list[str | None] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/token":
            return httpx2.Response(
                200,
                json={
                    "access_token": "new-access-secret",
                    "token_type": "Bearer",
                    "expires_in": 60,
                },
            )
        resource_authorization.append(request.headers.get("Authorization"))
        return httpx2.Response(200, json={"ok": True})

    async def exercise() -> None:
        await storage.set_tokens(old_tokens)
        await storage.set_client_info(client_info)
        provider.context.current_tokens = old_tokens
        provider.context.client_info = client_info
        provider.context.token_expiry_time = time.time() - 1
        provider._initialized = True
        async with httpx2.AsyncClient(
            transport=httpx2.MockTransport(handler),
            auth=provider,
        ) as client:
            response = await client.get("https://mcp.example.test/mcp")
        assert response.status_code == 200
        refreshed = await storage.get_tokens()
        assert refreshed is not None
        assert refreshed.access_token == "new-access-secret"

    asyncio.run(exercise())

    assert resource_authorization == ["Bearer new-access-secret"]


def test_official_provider_refresh_failure_is_redacted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from src.agent import mcp_client as registered_filters
    from src.agent.mcp_oauth import MCPOAuthFlowManager

    provider = MCPOAuthFlowManager(
        (_oauth_server(),)
    ).provider_for(_oauth_server())
    provider.context.current_tokens = OAuthToken(
        access_token="expired-access-secret",
        refresh_token="refresh-secret",
    )
    secret = "refresh-url-secret"
    response = httpx2.Response(
        400,
        request=httpx2.Request(
            "POST",
            f"https://login.example.test/token?value={secret}",
        ),
    )
    caplog.set_level(logging.DEBUG)

    async def exercise() -> bool:
        return await provider._handle_refresh_response(response)

    refreshed = asyncio.run(exercise())

    assert refreshed is False
    assert provider.context.current_tokens is None
    assert secret not in caplog.text
    assert "refresh-secret" not in caplog.text
    assert "MCP OAuth token refresh failed" in caplog.text
    assert registered_filters is not None


def test_oauth_failures_are_stable_and_logs_are_secret_free(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from config.settings import MCPStreamableHTTPServerConfig
    from src.agent import mcp_client
    from src.agent.mcp_client import MCPClientManager
    from src.agent.tools import ToolRegistry

    secret = "oauth-response-secret"
    server = MCPStreamableHTTPServerConfig(
        id="remote",
        transport="streamable_http",
        url="https://mcp.example.test/mcp",
        oauth={
            "redirect_uri": (
                "http://127.0.0.1:8000/agent/oauth/remote/callback"
            )
        },
    )

    @asynccontextmanager
    async def failing_client(
        config: object,
    ) -> AsyncIterator[object]:
        raise OAuthTokenError(secret)
        yield object()  # pragma: no cover

    manager = MCPClientManager([server], client_factory=failing_client)
    caplog.set_level(logging.DEBUG)
    sdk_logger = logging.getLogger("mcp.client.auth.oauth2")
    try:
        raise OAuthTokenError(secret)
    except OAuthTokenError:
        sdk_logger.exception("OAuth flow error")

    async def exercise() -> dict[str, object]:
        await manager.start(ToolRegistry())
        return dict(manager.snapshot()[0])

    snapshot = asyncio.run(exercise())

    assert snapshot["status"] == "unavailable"
    assert snapshot["error_code"] == "mcp_oauth_failed"
    assert secret not in caplog.text
    assert secret not in repr(snapshot)
    records = [
        record
        for record in caplog.records
        if record.name == "mcp.client.auth.oauth2"
    ]
    assert records
    assert all(record.exc_info is None for record in records)
    assert mcp_client is not None


@pytest.fixture
def oauth_api_client(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, list[dict[str, object]]]]:
    from src.agent import mcp_runtime as runtime_module
    from src.api import admin_auth

    api = _import_app(monkeypatch)
    calls: list[dict[str, object]] = []

    class FakeOAuthRuntime:
        async def oauth_status(self, server_id: str) -> dict[str, object]:
            calls.append({"action": "status", "server_id": server_id})
            return {
                "server_id": server_id,
                "status": "awaiting_callback",
                "authorization_url": (
                    "https://login.example.test/authorize?state=state-secret"
                ),
                "error_code": None,
            }

        async def complete_oauth_callback(
            self,
            server_id: str,
            *,
            code: str,
            state: str | None,
            issuer: str | None,
        ) -> None:
            calls.append(
                {
                    "action": "callback",
                    "server_id": server_id,
                    "code": code,
                    "state": state,
                    "issuer": issuer,
                }
            )

        async def cancel_oauth_authorization(
            self,
            server_id: str,
            *,
            state: str | None = None,
        ) -> None:
            calls.append(
                {
                    "action": "cancel",
                    "server_id": server_id,
                    "state": state,
                }
            )

    monkeypatch.setattr(runtime_module, "mcp_runtime", FakeOAuthRuntime())
    monkeypatch.setattr(
        admin_auth.settings,
        "admin_api_key",
        SecretStr("oauth-admin-key"),
    )
    client = TestClient(api.app)
    try:
        yield client, calls
    finally:
        client.close()


def test_oauth_admin_endpoints_are_protected_but_callback_is_public(
    oauth_api_client: tuple[TestClient, list[dict[str, object]]],
) -> None:
    client, calls = oauth_api_client

    unauthorized_status = client.get("/agent/oauth/remote")
    authorized_status = client.get(
        "/agent/oauth/remote",
        headers={"X-Admin-Key": "oauth-admin-key"},
    )
    callback = client.get(
        "/agent/oauth/remote/callback",
        params={
            "code": "authorization-code-secret",
            "state": "state-secret",
            "iss": "https://login.example.test",
        },
    )
    unauthorized_cancel = client.post("/agent/oauth/remote/cancel")
    authorized_cancel = client.post(
        "/agent/oauth/remote/cancel",
        headers={"X-Admin-Key": "oauth-admin-key"},
    )

    assert unauthorized_status.status_code == 401
    assert authorized_status.status_code == 200
    assert authorized_status.headers["cache-control"] == "no-store"
    assert callback.status_code == 202
    assert callback.json() == {"status": "received"}
    assert "authorization-code-secret" not in callback.text
    assert unauthorized_cancel.status_code == 401
    assert authorized_cancel.status_code == 202
    assert calls == [
        {"action": "status", "server_id": "remote"},
        {
            "action": "callback",
            "server_id": "remote",
            "code": "authorization-code-secret",
            "state": "state-secret",
            "issuer": "https://login.example.test",
        },
        {"action": "cancel", "server_id": "remote", "state": None},
    ]


def test_uvicorn_access_log_redacts_oauth_callback_query(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import h11
    from uvicorn.protocols.http.h11_impl import RequestResponseCycle

    _import_app(monkeypatch)
    access_logger = logging.getLogger("uvicorn.access")
    caplog.set_level(logging.INFO, logger="uvicorn.access")

    target = (
        "/agent/oauth/remote/callback"
        "?code=authorization-code-secret"
        "&state=state-secret&iss=issuer-secret"
    )

    async def emit_uvicorn_access_record() -> None:
        connection = h11.Connection(h11.SERVER)
        connection.receive_data(
            f"GET {target} HTTP/1.1\r\nHost: testserver\r\n\r\n".encode()
        )
        assert isinstance(connection.next_event(), h11.Request)
        cycle = RequestResponseCycle(
            scope={
                "type": "http",
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": "/agent/oauth/remote/callback",
                "raw_path": b"/agent/oauth/remote/callback",
                "query_string": target.partition("?")[2].encode(),
                "root_path": "",
                "headers": [(b"host", b"testserver")],
                "client": ("127.0.0.1", 43110),
                "server": ("127.0.0.1", 8000),
                "state": {},
                "extensions": {},
            },
            conn=connection,
            transport=SimpleNamespace(write=lambda data: None),
            flow=SimpleNamespace(write_paused=False),
            logger=logging.getLogger("uvicorn.error"),
            access_logger=access_logger,
            access_log=True,
            default_headers=[],
            message_event=asyncio.Event(),
            on_response=lambda: None,
        )
        await cycle.send(
            {"type": "http.response.start", "status": 202, "headers": []}
        )

    asyncio.run(emit_uvicorn_access_record())

    assert "GET /agent/oauth/remote/callback HTTP/1.1" in caplog.text
    assert "authorization-code-secret" not in caplog.text
    assert "state-secret" not in caplog.text
    assert "issuer-secret" not in caplog.text


def test_oauth_callback_rejects_ambiguous_secret_values_before_runtime(
    oauth_api_client: tuple[TestClient, list[dict[str, object]]],
) -> None:
    client, calls = oauth_api_client

    response = client.get(
        "/agent/oauth/remote/callback"
        "?code=first-secret&code=second-secret&state=state-secret"
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "mcp_oauth_callback_invalid"
    assert "first-secret" not in response.text
    assert "second-secret" not in response.text
    assert calls == []


def test_oauth_error_callback_requires_state_before_cancelling(
    oauth_api_client: tuple[TestClient, list[dict[str, object]]],
) -> None:
    client, calls = oauth_api_client

    response = client.get(
        "/agent/oauth/remote/callback",
        params={"error": "access_denied"},
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "mcp_oauth_callback_invalid"
    assert calls == []
