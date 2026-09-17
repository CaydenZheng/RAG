"""MCP client lifecycle orchestration with per-server failure isolation."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Literal, TypedDict

from loguru import logger
from mcp import Client

from config.settings import MCPServerConfig
from src.agent.tools import ToolRegistry

MCPServerStatus = Literal[
    "disabled",
    "connecting",
    "available",
    "degraded",
    "unavailable",
]
MCPTransport = Literal["stdio", "streamable_http"]


class MCPServerSnapshot(TypedDict):
    """Public, secret-free state for one configured MCP server."""

    id: str
    transport: MCPTransport
    status: MCPServerStatus
    error_code: str | None
    message: str


MCPClientFactory = Callable[
    [MCPServerConfig],
    AbstractAsyncContextManager[Client],
]


@dataclass
class _ServerConnection:
    close_requested: asyncio.Event
    owner_task: asyncio.Task[None]
    ready: asyncio.Future[Client]
    client: Client | None = None


class MCPClientManager:
    """Own configured MCP client contexts behind one small lifecycle interface."""

    def __init__(
        self,
        servers: Sequence[MCPServerConfig],
        *,
        client_factory: MCPClientFactory,
    ) -> None:
        self._servers: tuple[MCPServerConfig, ...] = tuple(servers)
        server_ids = [server.id for server in self._servers]
        if len(server_ids) != len(set(server_ids)):
            raise ValueError("MCP client manager requires unique server IDs")
        self._client_factory: MCPClientFactory = client_factory
        self._lifecycle_lock: asyncio.Lock = asyncio.Lock()
        self._state_lock: threading.RLock = threading.RLock()
        self._connections: dict[str, _ServerConnection] = {}
        self._tool_registry: ToolRegistry | None = None
        self._started: bool = False
        self._states: dict[str, MCPServerSnapshot] = {
            server.id: self._initial_state(server) for server in self._servers
        }

    async def start(self, tool_registry: ToolRegistry) -> None:
        """Connect every enabled server without letting one failure block others."""
        async with self._lifecycle_lock:
            if self._started:
                if tool_registry is not self._tool_registry:
                    raise RuntimeError(
                        "MCP client manager is already bound to another tool registry"
                    )
                return

            self._started = True
            self._tool_registry = tool_registry
            try:
                for server in self._servers:
                    self._set_provider_tools_available(
                        tool_registry,
                        server.id,
                        available=False,
                    )
                for server in self._servers:
                    if not server.enabled:
                        continue
                    self._update_state(
                        server.id,
                        status="connecting",
                        error_code=None,
                        message="server connection is starting",
                    )
                    await self._connect_server(server, tool_registry)
            except BaseException as error:
                await self._close_connections()
                if isinstance(error, asyncio.CancelledError):
                    error_code = "mcp_start_cancelled"
                    message = "server connection start was cancelled"
                else:
                    error_code = "mcp_start_aborted"
                    message = "server connection start was aborted"
                self._mark_connecting_unavailable(
                    error_code=error_code,
                    message=message,
                )
                self._started = False
                self._tool_registry = None
                raise

    async def close(self) -> None:
        """Close every active server independently and make provider tools unavailable."""
        async with self._lifecycle_lock:
            try:
                await self._close_connections()
            finally:
                self._started = False
                self._tool_registry = None

    def snapshot(self) -> tuple[MCPServerSnapshot, ...]:
        """Return configuration-ordered copies without commands, URLs, or secrets."""
        with self._state_lock:
            return tuple(self._states[server.id].copy() for server in self._servers)

    async def _connect_server(
        self,
        server: MCPServerConfig,
        tool_registry: ToolRegistry,
    ) -> None:
        try:
            client_context = self._client_factory(server)
        except Exception as error:
            self._update_state(
                server.id,
                status="unavailable",
                error_code="mcp_connect_failed",
                message="server connection failed",
            )
            logger.warning(
                "MCP server {} connection failed: {}",
                server.id,
                type(error).__name__,
            )
            return

        close_requested = asyncio.Event()
        ready: asyncio.Future[Client] = asyncio.get_running_loop().create_future()
        owner_task = asyncio.create_task(
            self._own_client_context(
                client_context,
                close_requested=close_requested,
                ready=ready,
            )
        )
        connection = _ServerConnection(
            close_requested=close_requested,
            owner_task=owner_task,
            ready=ready,
        )
        self._connections[server.id] = connection
        try:
            client = await asyncio.shield(ready)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await owner_task
            self._connections.pop(server.id, None)
            self._update_state(
                server.id,
                status="unavailable",
                error_code="mcp_connect_failed",
                message="server connection failed",
            )
            logger.warning(
                "MCP server {} connection failed: {}",
                server.id,
                type(error).__name__,
            )
            return

        connection.client = client
        self._set_provider_tools_available(
            tool_registry,
            server.id,
            available=True,
        )
        self._update_state(
            server.id,
            status="available",
            error_code=None,
            message="server connection is available",
        )

    @staticmethod
    async def _own_client_context(
        client_context: AbstractAsyncContextManager[Client],
        *,
        close_requested: asyncio.Event,
        ready: asyncio.Future[Client],
    ) -> None:
        try:
            async with client_context as client:
                if not ready.done():
                    ready.set_result(client)
                await close_requested.wait()
        except asyncio.CancelledError as error:
            if not ready.done():
                ready.set_exception(error)
            raise
        except Exception as error:
            if not ready.done():
                ready.set_exception(error)
                return
            raise

    async def _close_connections(self) -> None:
        tool_registry = self._tool_registry
        caller_task = asyncio.current_task()
        initial_cancellation_requests = (
            caller_task.cancelling() if caller_task is not None else 0
        )
        if tool_registry is not None:
            for server in self._servers:
                self._set_provider_tools_available(
                    tool_registry,
                    server.id,
                    available=False,
                )
        for server in self._servers:
            connection = self._connections.get(server.id)
            if connection is not None and connection.client is not None:
                self._update_state(
                    server.id,
                    status="unavailable",
                    error_code="mcp_client_closing",
                    message="server connection is closing",
                )

        cancelled_error: asyncio.CancelledError | None = None
        closed_server_ids: list[str] = []
        for server in reversed(self._servers):
            if not server.enabled:
                continue
            connection = self._connections.get(server.id)
            if connection is None:
                continue

            if connection.client is None:
                connection.owner_task.cancel()
            else:
                connection.close_requested.set()
            while not connection.owner_task.done():
                try:
                    await asyncio.shield(connection.owner_task)
                except asyncio.CancelledError as error:
                    if (
                        caller_task is not None
                        and caller_task.cancelling()
                        > initial_cancellation_requests
                        and cancelled_error is None
                    ):
                        cancelled_error = error
                except Exception:
                    break

            if connection.client is None and connection.ready.done():
                try:
                    connection.ready.exception()
                except asyncio.CancelledError:
                    pass
            try:
                connection.owner_task.result()
            except asyncio.CancelledError as error:
                if connection.client is not None:
                    if cancelled_error is None:
                        cancelled_error = error
                    self._update_state(
                        server.id,
                        status="unavailable",
                        error_code="mcp_close_cancelled",
                        message="server connection close was cancelled",
                    )
            except Exception as error:
                self._update_state(
                    server.id,
                    status="unavailable",
                    error_code="mcp_close_failed",
                    message="server connection could not be closed cleanly",
                )
                logger.warning(
                    "MCP server {} close failed: {}",
                    server.id,
                    type(error).__name__,
                )
            else:
                if connection.client is not None:
                    closed_server_ids.append(server.id)
                    self._update_state(
                        server.id,
                        status="unavailable",
                        error_code="mcp_client_closed",
                        message="server connection is closed",
                    )
            finally:
                self._connections.pop(server.id, None)

        if cancelled_error is not None:
            for server_id in closed_server_ids:
                self._update_state(
                    server_id,
                    status="unavailable",
                    error_code="mcp_close_cancelled",
                    message="server connection close was cancelled",
                )
            raise cancelled_error

    def _update_state(
        self,
        server_id: str,
        *,
        status: MCPServerStatus,
        error_code: str | None,
        message: str,
    ) -> None:
        with self._state_lock:
            state = self._states[server_id].copy()
            state.update(
                status=status,
                error_code=error_code,
                message=message,
            )
            self._states[server_id] = state

    def _mark_connecting_unavailable(
        self,
        *,
        error_code: str,
        message: str,
    ) -> None:
        with self._state_lock:
            for server_id, current in self._states.items():
                if current["status"] != "connecting":
                    continue
                state = current.copy()
                state.update(
                    status="unavailable",
                    error_code=error_code,
                    message=message,
                )
                self._states[server_id] = state

    @staticmethod
    def _set_provider_tools_available(
        tool_registry: ToolRegistry,
        server_id: str,
        *,
        available: bool,
    ) -> None:
        for tool in tool_registry.tools.values():
            if tool.source == "mcp" and tool.provider == server_id:
                tool.available = available

    @staticmethod
    def _initial_state(server: MCPServerConfig) -> MCPServerSnapshot:
        if not server.enabled:
            return {
                "id": server.id,
                "transport": server.transport,
                "status": "disabled",
                "error_code": None,
                "message": "server is disabled by configuration",
            }
        return {
            "id": server.id,
            "transport": server.transport,
            "status": "unavailable",
            "error_code": "mcp_not_started",
            "message": "server connection has not started",
        }
