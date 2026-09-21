"""Application-owned MCP lifecycle and public status projection."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Sequence
from typing import Literal, TypedDict

from loguru import logger

from config.settings import MCPServerConfig, settings
from src.agent.mcp_client import (
    MCPClientManager,
    MCPServerSnapshot,
)
from src.agent.mcp_oauth import MCPOAuthNotConfigured, MCPOAuthStatus
from src.agent.tools import ToolRegistry, ToolStatusSnapshot, tool_registry

MCPReadinessStatus = Literal["disabled", "starting", "ready", "degraded"]


class MCPReadinessSummary(TypedDict):
    """Small optional-component summary used by the readiness endpoint."""

    status: MCPReadinessStatus
    configured_servers: int
    enabled_servers: int
    available_servers: int
    degraded_servers: int


class MCPServerStatusView(TypedDict):
    """Secret-free server state with the number of registered tools."""

    id: str
    transport: str
    status: str
    error_code: str | None
    message: str
    tool_count: int


class MCPStatusPayload(TypedDict):
    """Public payload for the Agent tool status endpoint."""

    tools: list[ToolStatusSnapshot]
    mcp_servers: list[MCPServerStatusView]


MCPManagerFactory = Callable[[Sequence[MCPServerConfig]], MCPClientManager]


class MCPRuntime:
    """Bind one MCP manager to the application's shared tool registry."""

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        manager_factory: MCPManagerFactory = MCPClientManager,
    ) -> None:
        self._registry = registry
        self._manager_factory = manager_factory
        self._lifecycle_lock = asyncio.Lock()
        self._state_lock = threading.RLock()
        self._manager: MCPClientManager | None = None
        self._start_task: asyncio.Task[None] | None = None

    async def start(self, servers: Sequence[MCPServerConfig]) -> None:
        """Start configured servers and wait for their initial discovery."""
        start_task = await self._schedule_start(servers)
        await start_task

    async def start_background(
        self,
        servers: Sequence[MCPServerConfig],
    ) -> None:
        """Schedule optional MCP initialization without blocking app startup."""
        await self._schedule_start(servers)

    async def close(self) -> None:
        """Cancel pending initialization and close the active manager."""
        async with self._lifecycle_lock:
            await self._close_active()

    async def _schedule_start(
        self,
        servers: Sequence[MCPServerConfig],
    ) -> asyncio.Task[None]:
        async with self._lifecycle_lock:
            await self._close_active()
            manager = self._manager_factory(tuple(servers))
            start_task = asyncio.create_task(
                manager.start(self._registry),
                name="mcp-client-manager-start",
            )
            start_task.add_done_callback(self._report_start_failure)
            with self._state_lock:
                self._manager = manager
                self._start_task = start_task
            return start_task

    async def _close_active(self) -> None:
        with self._state_lock:
            manager = self._manager
            start_task = self._start_task
        if manager is None:
            return

        caller_task = asyncio.current_task()
        initial_cancellation_requests = (
            caller_task.cancelling() if caller_task is not None else 0
        )
        cancelled_error: asyncio.CancelledError | None = None
        if start_task is not None:
            if not start_task.done():
                start_task.cancel()
            try:
                await start_task
            except asyncio.CancelledError as error:
                if (
                    caller_task is not None
                    and caller_task.cancelling()
                    > initial_cancellation_requests
                ):
                    cancelled_error = error
            except Exception:
                pass

        try:
            await manager.close()
        except asyncio.CancelledError as error:
            if cancelled_error is None:
                cancelled_error = error
        finally:
            with self._state_lock:
                if self._manager is manager:
                    self._manager = None
                if self._start_task is start_task:
                    self._start_task = None

        if cancelled_error is not None:
            raise cancelled_error

    @staticmethod
    def _report_start_failure(start_task: asyncio.Task[None]) -> None:
        if start_task.cancelled():
            return
        error = start_task.exception()
        if error is not None:
            logger.warning(
                "MCP manager background start failed: {}",
                type(error).__name__,
            )

    def server_snapshot(self) -> tuple[MCPServerSnapshot, ...]:
        """Return the current manager snapshot, or an empty pre-start state."""
        with self._state_lock:
            manager = self._manager
        return manager.snapshot() if manager is not None else ()

    def readiness_summary(self) -> MCPReadinessSummary:
        """Summarize optional MCP health without exposing server configuration."""
        states = self.server_snapshot()
        enabled = [state for state in states if state["status"] != "disabled"]
        if not enabled:
            status: MCPReadinessStatus = "disabled"
        elif any(state["status"] == "connecting" for state in enabled):
            status = "starting"
        elif all(state["status"] == "available" for state in enabled):
            status = "ready"
        else:
            status = "degraded"

        return {
            "status": status,
            "configured_servers": len(states),
            "enabled_servers": len(enabled),
            "available_servers": sum(
                state["status"] == "available" for state in enabled
            ),
            "degraded_servers": sum(
                state["status"] in {"degraded", "unavailable"}
                for state in enabled
            ),
        }

    def status_payload(self) -> MCPStatusPayload:
        """Combine public tool metadata with secret-free provider state."""
        tools = list(self._registry.status_snapshot())
        tool_counts: dict[str, int] = {}
        for tool in tools:
            if tool["source"] == "mcp" and tool["provider"] is not None:
                provider = tool["provider"]
                tool_counts[provider] = tool_counts.get(provider, 0) + 1

        servers: list[MCPServerStatusView] = [
            {
                "id": state["id"],
                "transport": state["transport"],
                "status": state["status"],
                "error_code": state["error_code"],
                "message": state["message"],
                "tool_count": tool_counts.get(state["id"], 0),
            }
            for state in self.server_snapshot()
        ]
        return {"tools": tools, "mcp_servers": servers}

    async def oauth_status(self, server_id: str) -> MCPOAuthStatus:
        """Return admin-only OAuth state from the active manager."""
        manager = self._active_manager()
        return await manager.oauth_status(server_id)

    async def complete_oauth_callback(
        self,
        server_id: str,
        *,
        code: str,
        state: str | None,
        issuer: str | None,
    ) -> None:
        """Deliver an OAuth callback to the active manager."""
        manager = self._active_manager()
        await manager.complete_oauth_callback(
            server_id,
            code=code,
            state=state,
            issuer=issuer,
        )

    async def cancel_oauth_authorization(
        self,
        server_id: str,
        *,
        state: str | None = None,
    ) -> None:
        """Cancel a pending OAuth authorization on the active manager."""
        manager = self._active_manager()
        await manager.cancel_oauth_authorization(server_id, state=state)

    def _active_manager(self) -> MCPClientManager:
        with self._state_lock:
            manager = self._manager
        if manager is None:
            raise MCPOAuthNotConfigured()
        return manager


mcp_runtime = MCPRuntime(tool_registry)


async def start_mcp_runtime() -> None:
    """Schedule configured MCP servers during the ASGI startup lifecycle."""
    await mcp_runtime.start_background(settings.mcp_servers)


async def close_mcp_runtime() -> None:
    """Close configured MCP servers during the ASGI shutdown lifecycle."""
    await mcp_runtime.close()
