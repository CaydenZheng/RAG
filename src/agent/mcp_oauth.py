"""Interactive OAuth coordination around the official MCP SDK provider."""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Callable, Sequence
from typing import Literal, Protocol, TypedDict
from urllib.parse import parse_qs, urlparse

from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.shared.auth import (
    AuthorizationCodeResult,
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthToken,
)

from config.settings import MCPServerConfig, MCPStreamableHTTPServerConfig


class MCPOAuthNotConfigured(LookupError):
    """The requested MCP server has no OAuth configuration."""


class MCPOAuthCallbackNotPending(RuntimeError):
    """No authorization callback is currently expected."""


class MCPOAuthCallbackStateMismatch(ValueError):
    """The callback state does not match the pending authorization."""


class MCPOAuthAuthorizationCancelled(RuntimeError):
    """The pending interactive authorization was cancelled locally."""


class MCPOAuthFlowSetupError(RuntimeError):
    """The SDK did not provide a usable authorization request."""


class OAuthCredentialStorage(TokenStorage, Protocol):
    """Replaceable SDK storage boundary for one configured MCP server."""


OAuthStorageFactory = Callable[[str], OAuthCredentialStorage]
MCPOAuthStatusName = Literal[
    "idle",
    "awaiting_callback",
    "callback_received",
    "authorized",
    "cancelled",
]


class MCPOAuthStatus(TypedDict):
    """Admin-only OAuth state; it never contains tokens or authorization codes."""

    server_id: str
    status: MCPOAuthStatusName
    authorization_url: str | None
    error_code: str | None


class InMemoryOAuthTokenStorage:
    """Process-local development storage; production stores can replace this seam."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._tokens: OAuthToken | None = None
        self._client_info: OAuthClientInformationFull | None = None

    async def get_tokens(self) -> OAuthToken | None:
        async with self._lock:
            return (
                self._tokens.model_copy(deep=True)
                if self._tokens is not None
                else None
            )

    async def set_tokens(self, tokens: OAuthToken) -> None:
        async with self._lock:
            self._tokens = tokens.model_copy(deep=True)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        async with self._lock:
            return (
                self._client_info.model_copy(deep=True)
                if self._client_info is not None
                else None
            )

    async def set_client_info(
        self,
        client_info: OAuthClientInformationFull,
    ) -> None:
        async with self._lock:
            self._client_info = client_info.model_copy(deep=True)


def _create_in_memory_storage(server_id: str) -> OAuthCredentialStorage:
    del server_id
    return InMemoryOAuthTokenStorage()


class _MCPOAuthSession:
    """Coordinate one SDK redirect/callback pair without exposing secrets."""

    def __init__(
        self,
        server: MCPStreamableHTTPServerConfig,
        storage: OAuthCredentialStorage,
    ) -> None:
        if server.oauth is None:
            raise ValueError("OAuth session requires OAuth configuration")
        self._server = server
        self._storage = storage
        self._lock = asyncio.Lock()
        self._callback: asyncio.Future[AuthorizationCodeResult] | None = None
        self._authorization_url: str | None = None
        self._expected_state: str | None = None
        self._last_error_code: str | None = None

    def build_provider(self) -> OAuthClientProvider:
        oauth = self._server.oauth
        if oauth is None:  # pragma: no cover - constructor enforces this
            raise ValueError("OAuth configuration is missing")
        metadata = OAuthClientMetadata(
            redirect_uris=[oauth.redirect_uri],
            client_name=oauth.client_name,
            scope=oauth.scope,
            application_type=oauth.application_type,
        )
        return OAuthClientProvider(
            server_url=str(self._server.url),
            client_metadata=metadata,
            storage=self._storage,
            redirect_handler=self._receive_authorization_url,
            callback_handler=self._wait_for_callback,
            client_metadata_url=(
                str(oauth.client_metadata_url)
                if oauth.client_metadata_url is not None
                else None
            ),
        )

    async def status(self) -> MCPOAuthStatus:
        tokens = await self._storage.get_tokens()
        async with self._lock:
            callback = self._callback
            if callback is not None and not callback.done():
                status: MCPOAuthStatusName = "awaiting_callback"
            elif callback is not None:
                status = "callback_received"
            elif tokens is not None:
                status = "authorized"
            elif self._last_error_code is not None:
                status = "cancelled"
            else:
                status = "idle"
            return {
                "server_id": self._server.id,
                "status": status,
                "authorization_url": self._authorization_url,
                "error_code": self._last_error_code,
            }

    async def complete(
        self,
        *,
        code: str,
        state: str | None,
        issuer: str | None,
    ) -> None:
        async with self._lock:
            callback = self._pending_callback()
            self._validate_state(state)
            callback.set_result(
                AuthorizationCodeResult(code=code, state=state, iss=issuer)
            )
            self._last_error_code = None

    async def cancel(self, *, state: str | None = None) -> None:
        async with self._lock:
            callback = self._pending_callback()
            if state is not None:
                self._validate_state(state)
            callback.set_exception(MCPOAuthAuthorizationCancelled())
            self._last_error_code = "mcp_oauth_cancelled"

    async def _receive_authorization_url(self, authorization_url: str) -> None:
        states = parse_qs(
            urlparse(authorization_url).query,
            keep_blank_values=True,
        ).get("state", [])
        if len(states) != 1 or not states[0]:
            raise MCPOAuthFlowSetupError()
        async with self._lock:
            if self._callback is not None and not self._callback.done():
                raise MCPOAuthFlowSetupError()
            self._callback = asyncio.get_running_loop().create_future()
            self._authorization_url = authorization_url
            self._expected_state = states[0]
            self._last_error_code = None

    async def _wait_for_callback(self) -> AuthorizationCodeResult:
        async with self._lock:
            callback = self._callback
        if callback is None:
            raise MCPOAuthFlowSetupError()
        try:
            return await callback
        finally:
            async with self._lock:
                if self._callback is callback:
                    self._callback = None
                    self._authorization_url = None
                    self._expected_state = None

    def _pending_callback(self) -> asyncio.Future[AuthorizationCodeResult]:
        callback = self._callback
        if callback is None or callback.done():
            raise MCPOAuthCallbackNotPending()
        return callback

    def _validate_state(self, state: str | None) -> None:
        expected = self._expected_state
        if (
            expected is None
            or state is None
            or not secrets.compare_digest(state, expected)
        ):
            raise MCPOAuthCallbackStateMismatch()


class MCPOAuthFlowManager:
    """Own OAuth providers and interaction state for configured HTTP servers."""

    def __init__(
        self,
        servers: Sequence[MCPServerConfig],
        *,
        storage_factory: OAuthStorageFactory = _create_in_memory_storage,
    ) -> None:
        self._sessions: dict[str, _MCPOAuthSession] = {}
        for server in servers:
            if (
                isinstance(server, MCPStreamableHTTPServerConfig)
                and server.oauth is not None
            ):
                self._sessions[server.id] = _MCPOAuthSession(
                    server,
                    storage_factory(server.id),
                )

    def provider_for(
        self,
        server: MCPStreamableHTTPServerConfig,
    ) -> OAuthClientProvider:
        return self._session(server.id).build_provider()

    async def status(self, server_id: str) -> MCPOAuthStatus:
        return await self._session(server_id).status()

    async def complete_callback(
        self,
        server_id: str,
        *,
        code: str,
        state: str | None,
        issuer: str | None,
    ) -> None:
        await self._session(server_id).complete(
            code=code,
            state=state,
            issuer=issuer,
        )

    async def cancel_authorization(
        self,
        server_id: str,
        *,
        state: str | None = None,
    ) -> None:
        await self._session(server_id).cancel(state=state)

    def _session(self, server_id: str) -> _MCPOAuthSession:
        try:
            return self._sessions[server_id]
        except KeyError as error:
            raise MCPOAuthNotConfigured() from error
