"""MCP client lifecycle orchestration with per-server failure isolation."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections.abc import AsyncIterator, Callable, Iterable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal, TypedDict

import httpx2
from loguru import logger
from mcp import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import TextContent
from mcp.types import Tool as MCPTool

from config.settings import (
    MCPServerConfig,
    MCPStdioServerConfig,
    MCPStreamableHTTPServerConfig,
)
from src.agent.tool_schema import InvalidToolSchema, ensure_safe_input_schema
from src.agent.tools import (
    SafetyLevel,
    ToolDef,
    ToolRegistry,
    tool_params_from_schema,
)
from src.core.agent_runtime import ToolResult

MCPServerStatus = Literal[
    "disabled",
    "connecting",
    "available",
    "degraded",
    "unavailable",
]
MCPTransport = Literal["stdio", "streamable_http"]
MAX_MCP_TOOL_PAGES = 100
MAX_MCP_TOOLS_PER_SERVER = 1000


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


class _SafeStdioProtocolLogFilter(logging.Filter):
    """Remove peer-controlled parse details before logging handlers run."""

    def filter(self, record: logging.LogRecord) -> bool:
        if (
            record.name == "mcp.client.stdio"
            and record.msg == "Failed to parse JSONRPC message from server"
        ):
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        return True


_STDIO_PROTOCOL_LOG_FILTER = _SafeStdioProtocolLogFilter()
logging.getLogger("mcp.client.stdio").addFilter(_STDIO_PROTOCOL_LOG_FILTER)


class _SafeStreamableHTTPLogFilter(logging.Filter):
    """Keep SDK diagnostics while removing peer- and request-controlled data."""

    _SAFE_MESSAGES: tuple[tuple[str, str], ...] = (
        (
            "Connecting to StreamableHTTP endpoint:",
            "Connecting to StreamableHTTP endpoint",
        ),
        ("Received session ID:", "Received StreamableHTTP session ID"),
        ("SSE message:", "Received StreamableHTTP SSE message"),
        ("Unknown SSE event:", "Received unknown StreamableHTTP SSE event"),
        ("GET stream not opened:", "StreamableHTTP GET stream was not opened"),
        ("Sending client message:", "Sending StreamableHTTP client message"),
        ("Unexpected content type:", "Received unexpected HTTP content type"),
        ("Reconnection failed:", "StreamableHTTP reconnection failed"),
        ("Session termination failed:", "StreamableHTTP session termination failed"),
    )

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != "mcp.client.streamable_http":
            return True

        message = str(record.msg)
        for prefix, replacement in self._SAFE_MESSAGES:
            if message.startswith(prefix):
                record.msg = replacement
                record.args = ()
                break
        else:
            record.msg = "MCP Streamable HTTP transport event"
            record.args = ()

        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


class _SafeHTTPXRequestLogFilter(logging.Filter):
    """Remove endpoint URLs from the HTTP client's standard request log."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name == "httpx2" and str(record.msg).startswith("HTTP Request:"):
            record.msg = "HTTP request completed"
            record.args = ()
        return True


class _SafeHTTPCoreLogFilter(logging.Filter):
    """Remove peer-controlled connection and response details from debug logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name.startswith("httpcore2."):
            record.msg = "HTTP transport event"
            record.args = ()
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        return True


_STREAMABLE_HTTP_LOG_FILTER = _SafeStreamableHTTPLogFilter()
logging.getLogger("mcp.client.streamable_http").addFilter(
    _STREAMABLE_HTTP_LOG_FILTER
)
_HTTPX_REQUEST_LOG_FILTER = _SafeHTTPXRequestLogFilter()
logging.getLogger("httpx2").addFilter(_HTTPX_REQUEST_LOG_FILTER)
_HTTPCORE_LOG_FILTER = _SafeHTTPCoreLogFilter()
for _logger_name in (
    "httpcore2.connection",
    "httpcore2.http11",
    "httpcore2.http2",
    "httpcore2.proxy",
    "httpcore2.socks",
):
    logging.getLogger(_logger_name).addFilter(_HTTPCORE_LOG_FILTER)


@asynccontextmanager
async def _stdio_client_context(
    server: MCPStdioServerConfig,
) -> AsyncIterator[Client]:
    """Enter the official stdio transport without exposing peer output."""
    environment = (
        {
            name: value.get_secret_value()
            for name, value in server.env.items()
        }
        if server.env is not None
        else None
    )
    parameters = StdioServerParameters(
        command=server.command,
        args=list(server.args),
        env=environment,
        cwd=server.cwd,
    )
    with open(os.devnull, "w", encoding="utf-8") as error_sink:
        transport = stdio_client(parameters, errlog=error_sink)
        async with Client(transport) as client:
            yield client


@asynccontextmanager
async def _streamable_http_client_context(
    server: MCPStreamableHTTPServerConfig,
) -> AsyncIterator[Client]:
    """Enter the official Streamable HTTP transport with configured timeouts."""
    timeout = httpx2.Timeout(
        server.timeout_seconds,
        read=server.read_timeout_seconds,
    )
    async with httpx2.AsyncClient(timeout=timeout) as http_client:
        transport = streamable_http_client(
            str(server.url),
            http_client=http_client,
        )
        async with Client(transport) as client:
            yield client


def _default_client_factory(
    server: MCPServerConfig,
) -> AbstractAsyncContextManager[Client]:
    """Build an official SDK client while keeping transports behind one seam."""
    if isinstance(server, MCPStdioServerConfig):
        return _stdio_client_context(server)
    if isinstance(server, MCPStreamableHTTPServerConfig):
        return _streamable_http_client_context(server)
    raise ValueError("MCP transport is not implemented")


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
        client_factory: MCPClientFactory = _default_client_factory,
    ) -> None:
        self._servers: tuple[MCPServerConfig, ...] = tuple(servers)
        server_ids = [server.id for server in self._servers]
        if len(server_ids) != len(set(server_ids)):
            raise ValueError("MCP client manager requires unique server IDs")
        self._client_factory: MCPClientFactory = client_factory
        self._lifecycle_lock: asyncio.Lock = asyncio.Lock()
        self._state_lock: threading.RLock = threading.RLock()
        self._connections: dict[str, _ServerConnection] = {}
        self._discovery_registry: ToolRegistry | None = None
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

            self._bind_discovery_registry(tool_registry)
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
                interrupted_server_ids = self._server_ids_with_status(
                    "connecting"
                )
                await self._close_connections()
                if isinstance(error, asyncio.CancelledError):
                    error_code = "mcp_start_cancelled"
                    message = "server connection start was cancelled"
                else:
                    error_code = "mcp_start_aborted"
                    message = "server connection start was aborted"
                self._mark_servers_unavailable(
                    interrupted_server_ids,
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
        try:
            (
                registered_tool_names,
                rejected_tool_count,
            ) = await self._discover_server_tools(
                server,
                client,
                tool_registry,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self._close_after_discovery_failure(server.id, connection)
            self._update_state(
                server.id,
                status="unavailable",
                error_code="mcp_tool_discovery_failed",
                message="server tool discovery failed",
            )
            logger.warning(
                "MCP server {} tool discovery failed: {}",
                server.id,
                type(error).__name__,
            )
            return

        self._set_tools_available(
            tool_registry,
            registered_tool_names,
            available=True,
        )
        if rejected_tool_count:
            self._update_state(
                server.id,
                status="degraded",
                error_code="mcp_tool_definition_rejected",
                message="one or more server tool definitions were rejected",
            )
        else:
            self._update_state(
                server.id,
                status="available",
                error_code=None,
                message="server connection is available",
            )

    async def _discover_server_tools(
        self,
        server: MCPServerConfig,
        client: Client,
        tool_registry: ToolRegistry,
    ) -> tuple[set[str], int]:
        discovered_tools = await self._list_server_tools(client)
        self._unregister_provider_tools(tool_registry, server.id)

        registered_names: set[str] = set()
        seen_names: set[str] = set()
        rejected_tool_count = 0
        for mcp_tool in discovered_tools:
            registered_name = f"mcp__{server.id}__{mcp_tool.name}"
            if registered_name in seen_names:
                rejected_tool_count += 1
                logger.warning(
                    "MCP server {} returned a duplicate tool name",
                    server.id,
                )
                continue
            seen_names.add(registered_name)
            try:
                tool_registry.register(
                    self._build_tool_def(
                        server_id=server.id,
                        registered_name=registered_name,
                        mcp_tool=mcp_tool,
                        client=client,
                    )
                )
            except InvalidToolSchema as error:
                rejected_tool_count += 1
                logger.warning(
                    "MCP server {} tool definition was rejected: {}",
                    server.id,
                    str(error),
                )
                continue
            except (TypeError, ValueError) as error:
                rejected_tool_count += 1
                logger.warning(
                    "MCP server {} tool definition was rejected: {}",
                    server.id,
                    type(error).__name__,
                )
                continue
            registered_names.add(registered_name)

        return registered_names, rejected_tool_count

    @staticmethod
    async def _list_server_tools(client: Client) -> list[MCPTool]:
        tools: list[MCPTool] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for _ in range(MAX_MCP_TOOL_PAGES):
            result = await client.list_tools(
                cursor=cursor,
                cache_mode="refresh",
            )
            if len(tools) + len(result.tools) > MAX_MCP_TOOLS_PER_SERVER:
                raise RuntimeError("MCP tool discovery exceeded the tool limit")
            tools.extend(result.tools)
            next_cursor = result.next_cursor
            if next_cursor is None:
                return tools
            if next_cursor in seen_cursors:
                raise RuntimeError("MCP tool discovery returned a cursor cycle")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        raise RuntimeError("MCP tool discovery exceeded the page limit")

    @staticmethod
    def _build_tool_def(
        *,
        server_id: str,
        registered_name: str,
        mcp_tool: MCPTool,
        client: Client,
    ) -> ToolDef:
        # Protect Planner projection; ToolRegistry revalidates its boundary.
        ensure_safe_input_schema(mcp_tool.input_schema)
        input_schema = deepcopy(mcp_tool.input_schema)
        params, warnings = tool_params_from_schema(input_schema)

        def execute_sync(arguments: dict[str, Any]) -> ToolResult:
            return ToolResult(
                success=False,
                error="MCP tools require asynchronous execution",
                error_code="mcp_async_required",
                tool_name=registered_name,
            )

        async def execute_async(arguments: dict[str, Any]) -> ToolResult:
            try:
                result = await client.call_tool(mcp_tool.name, arguments)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning(
                    "MCP server {} tool call failed: {}",
                    server_id,
                    type(error).__name__,
                )
                return ToolResult(
                    success=False,
                    error="MCP tool call failed",
                    error_code="mcp_call_failed",
                    tool_name=registered_name,
                )
            if result.is_error:
                return ToolResult(
                    success=False,
                    error="MCP tool returned an error",
                    error_code="mcp_tool_error",
                    tool_name=registered_name,
                )
            if result.structured_content is not None:
                data = result.structured_content
            else:
                text = "\n".join(
                    block.text
                    for block in result.content
                    if isinstance(block, TextContent)
                )
                data = {"text": text}
            return ToolResult(
                success=True,
                data=data,
                tool_name=registered_name,
            )

        return ToolDef(
            name=registered_name,
            description=mcp_tool.description or mcp_tool.title or mcp_tool.name,
            params=params,
            safety_level=SafetyLevel.GRAYLIST,
            execute_fn=execute_sync,
            execute_async_fn=execute_async,
            category="mcp",
            max_retries=0,
            source="mcp",
            provider=server_id,
            input_schema=input_schema,
            schema_warnings=warnings,
            available=False,
        )

    async def _close_after_discovery_failure(
        self,
        server_id: str,
        connection: _ServerConnection,
    ) -> None:
        connection.close_requested.set()
        try:
            await asyncio.shield(connection.owner_task)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning(
                "MCP server {} close after discovery failure failed: {}",
                server_id,
                type(error).__name__,
            )
        self._connections.pop(server_id, None)

    def _bind_discovery_registry(self, tool_registry: ToolRegistry) -> None:
        previous_registry = self._discovery_registry
        if previous_registry is not None and previous_registry is not tool_registry:
            for server in self._servers:
                self._unregister_provider_tools(previous_registry, server.id)
        self._discovery_registry = tool_registry

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

    def _server_ids_with_status(
        self,
        status: MCPServerStatus,
    ) -> tuple[str, ...]:
        with self._state_lock:
            return tuple(
                server_id
                for server_id, current in self._states.items()
                if current["status"] == status
            )

    def _mark_servers_unavailable(
        self,
        server_ids: Sequence[str],
        *,
        error_code: str,
        message: str,
    ) -> None:
        with self._state_lock:
            for server_id in server_ids:
                state = self._states[server_id].copy()
                state.update(
                    status="unavailable",
                    error_code=error_code,
                    message=message,
                )
                self._states[server_id] = state

    @staticmethod
    def _unregister_provider_tools(
        tool_registry: ToolRegistry,
        server_id: str,
    ) -> None:
        names = [
            name
            for name, tool in tool_registry.tools.items()
            if tool.source == "mcp" and tool.provider == server_id
        ]
        for name in names:
            tool_registry.unregister(name)

    @staticmethod
    def _set_tools_available(
        tool_registry: ToolRegistry,
        names: Iterable[str],
        *,
        available: bool,
    ) -> None:
        for name in names:
            tool = tool_registry.get_tool(name)
            if tool is not None:
                tool.available = available

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
