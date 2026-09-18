"""MCP Client Manager owns optional server lifecycles without cross-failure."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import TYPE_CHECKING, cast

import pytest
from mcp import Client
from mcp.server.mcpserver import MCPServer

if TYPE_CHECKING:
    from config.settings import MCPServerConfig, MCPStdioServerConfig
    from src.agent.mcp_client import (
        MCPClientFactory,
        MCPClientManager,
        MCPServerSnapshot,
    )
    from src.agent.tools import ToolDef, ToolRegistry


def _server(server_id: str, *, enabled: bool = True) -> MCPStdioServerConfig:
    from config.settings import MCPStdioServerConfig

    return MCPStdioServerConfig(
        id=server_id,
        enabled=enabled,
        transport="stdio",
        command="unused-in-unit-tests",
    )


def _mcp_tool(provider: str) -> ToolDef:
    from src.agent.tools import SafetyLevel, ToolDef
    from src.core.agent_runtime import ToolResult

    return ToolDef(
        name=f"mcp__{provider}__probe",
        description="MCP lifecycle probe",
        params=[],
        safety_level=SafetyLevel.GRAYLIST,
        execute_fn=lambda params: ToolResult(success=True),
        source="mcp",
        provider=provider,
        max_retries=0,
    )


def _registry() -> ToolRegistry:
    from src.agent.tools import ToolRegistry

    return ToolRegistry()


def _manager(
    servers: list[MCPServerConfig],
    *,
    client_factory: MCPClientFactory,
) -> MCPClientManager:
    from src.agent.mcp_client import MCPClientManager

    return MCPClientManager(servers, client_factory=client_factory)


def test_manager_owns_official_client_lifecycle_and_can_restart() -> None:
    server = MCPServer("manager-lifecycle-test")
    configured = _server("clock")
    registry = _registry()
    created: list[str] = []

    def create_client(config: MCPServerConfig) -> Client:
        created.append(config.id)
        return Client(server)

    manager = _manager([configured], client_factory=create_client)

    async def exercise() -> tuple[tuple[MCPServerSnapshot, ...], ...]:
        await manager.start(registry)
        first_started = manager.snapshot()
        await manager.start(registry)
        await manager.close()
        first_closed = manager.snapshot()
        await manager.close()
        await manager.start(registry)
        second_started = manager.snapshot()
        await manager.close()
        return first_started, first_closed, second_started

    first_started, first_closed, second_started = asyncio.run(exercise())

    assert first_started[0]["status"] == "available"
    assert first_closed[0]["error_code"] == "mcp_client_closed"
    assert second_started[0]["status"] == "available"
    assert created == ["clock", "clock"]


def test_empty_manager_has_no_lifecycle_side_effects() -> None:
    def unexpected_client(config: MCPServerConfig) -> Client:
        raise AssertionError(f"empty manager reached factory: {config.id}")

    manager = _manager([], client_factory=unexpected_client)

    async def exercise() -> None:
        await manager.start(_registry())
        await manager.close()

    asyncio.run(exercise())

    assert manager.snapshot() == ()


def test_snapshot_exposes_connecting_transition() -> None:
    @asynccontextmanager
    async def client_context(
        entered: asyncio.Event,
        release: asyncio.Event,
    ) -> AsyncIterator[Client]:
        entered.set()
        await release.wait()
        yield cast(Client, object())

    async def exercise() -> tuple[str, str]:
        entered = asyncio.Event()
        release = asyncio.Event()
        manager = _manager(
            [_server("slow")],
            client_factory=lambda config: client_context(entered, release),
        )
        start_task = asyncio.create_task(manager.start(_registry()))
        await entered.wait()
        connecting = manager.snapshot()[0]["status"]
        release.set()
        await start_task
        available = manager.snapshot()[0]["status"]
        await manager.close()
        return connecting, available

    assert asyncio.run(exercise()) == ("connecting", "available")


def test_cancelled_start_resets_connecting_state_and_can_restart() -> None:
    async def exercise() -> tuple[dict[str, object], dict[str, object]]:
        entered = asyncio.Event()
        release = asyncio.Event()
        attempts = 0

        @asynccontextmanager
        async def client_context() -> AsyncIterator[Client]:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                entered.set()
                await release.wait()
            yield cast(Client, object())

        manager = _manager(
            [_server("cancelled")],
            client_factory=lambda config: client_context(),
        )
        registry = _registry()
        start_task = asyncio.create_task(manager.start(registry))
        await entered.wait()
        start_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await start_task
        after_cancel = dict(manager.snapshot()[0])

        await manager.start(registry)
        after_restart = dict(manager.snapshot()[0])
        await manager.close()
        return after_cancel, after_restart

    after_cancel, after_restart = asyncio.run(exercise())

    assert after_cancel["status"] == "unavailable"
    assert after_cancel["error_code"] == "mcp_start_cancelled"
    assert after_restart["status"] == "available"


def test_all_provider_tools_are_disabled_before_first_connection_await() -> None:
    first_tool = _mcp_tool("first")
    second_tool = _mcp_tool("second")
    registry = _registry()
    registry.register(first_tool)
    registry.register(second_tool)

    async def exercise() -> tuple[bool, bool, dict[str, object]]:
        first_entered = asyncio.Event()
        release_first = asyncio.Event()

        @asynccontextmanager
        async def client_context(config: MCPServerConfig) -> AsyncIterator[Client]:
            if config.id == "first":
                first_entered.set()
                await release_first.wait()
            yield cast(Client, object())

        manager = _manager(
            [_server("first"), _server("second")],
            client_factory=client_context,
        )
        start_task = asyncio.create_task(manager.start(registry))
        await first_entered.wait()
        availability = (first_tool.available, second_tool.available)
        second_state = dict(manager.snapshot()[1])
        release_first.set()
        await start_task
        await manager.close()
        return *availability, second_state

    first_available, second_available, second_state = asyncio.run(exercise())

    assert first_available is False
    assert second_available is False
    assert second_state["status"] == "unavailable"
    assert second_state["error_code"] == "mcp_not_started"


def test_one_connection_failure_does_not_block_other_servers() -> None:
    registry = _registry()
    working_tool = _mcp_tool("working")
    broken_tool = _mcp_tool("broken")
    second_tool = _mcp_tool("second")
    for tool in (working_tool, broken_tool, second_tool):
        registry.register(tool)
    events: list[str] = []

    def create_client(
        config: MCPServerConfig,
    ) -> AbstractAsyncContextManager[Client]:
        @asynccontextmanager
        async def client_context() -> AsyncIterator[Client]:
            events.append(f"enter:{config.id}")
            if config.id == "broken":
                raise RuntimeError("secret-token-must-not-leak")
            try:
                yield cast(Client, object())
            finally:
                events.append(f"exit:{config.id}")

        return client_context()

    manager = _manager(
        [_server("working"), _server("broken"), _server("second")],
        client_factory=create_client,
    )

    async def exercise() -> tuple[
        dict[str, dict[str, object]],
        tuple[bool, bool, bool],
        dict[str, dict[str, object]],
    ]:
        await manager.start(registry)
        started = {state["id"]: dict(state) for state in manager.snapshot()}
        availability = (
            working_tool.available,
            broken_tool.available,
            second_tool.available,
        )
        await manager.close()
        closed = {state["id"]: dict(state) for state in manager.snapshot()}
        return started, availability, closed

    started, availability, closed = asyncio.run(exercise())

    assert started["working"]["status"] == "available"
    assert started["second"]["status"] == "available"
    assert started["broken"] == {
        "id": "broken",
        "transport": "stdio",
        "status": "unavailable",
        "error_code": "mcp_connect_failed",
        "message": "server connection failed",
    }
    assert "secret-token-must-not-leak" not in repr(started)
    assert availability == (True, False, True)
    assert working_tool.available is False
    assert broken_tool.available is False
    assert second_tool.available is False
    assert closed["working"]["error_code"] == "mcp_client_closed"
    assert closed["broken"]["error_code"] == "mcp_connect_failed"
    assert closed["second"]["error_code"] == "mcp_client_closed"
    assert events == [
        "enter:working",
        "enter:broken",
        "enter:second",
        "exit:second",
        "exit:working",
    ]


def test_disabled_server_is_not_connected_and_snapshot_is_isolated() -> None:
    calls: list[str] = []
    registry = _registry()
    disabled_tool = _mcp_tool("disabled")
    registry.register(disabled_tool)

    def unexpected_client(config: MCPServerConfig) -> Client:
        calls.append(config.id)
        raise AssertionError("disabled server must not create a client")

    manager = _manager(
        [_server("disabled", enabled=False)],
        client_factory=unexpected_client,
    )

    async def exercise() -> None:
        await manager.start(registry)
        await manager.close()

    asyncio.run(exercise())
    snapshot = manager.snapshot()
    snapshot[0]["message"] = "caller mutation"

    assert calls == []
    assert disabled_tool.available is False
    assert manager.snapshot()[0] == {
        "id": "disabled",
        "transport": "stdio",
        "status": "disabled",
        "error_code": None,
        "message": "server is disabled by configuration",
    }


def test_manager_rejects_duplicate_server_ids() -> None:
    def unexpected_client(config: MCPServerConfig) -> Client:
        raise AssertionError(f"duplicate configuration reached factory: {config.id}")

    with pytest.raises(ValueError, match="unique server IDs"):
        _manager(
            [_server("duplicate"), _server("duplicate")],
            client_factory=unexpected_client,
        )


def test_close_failure_is_isolated_and_redacted() -> None:
    closed: list[str] = []

    def create_client(
        config: MCPServerConfig,
    ) -> AbstractAsyncContextManager[Client]:
        @asynccontextmanager
        async def client_context() -> AsyncIterator[Client]:
            try:
                yield cast(Client, object())
            finally:
                closed.append(config.id)
                if config.id == "broken-close":
                    raise RuntimeError("secret-close-detail")

        return client_context()

    manager = _manager(
        [_server("working-close"), _server("broken-close")],
        client_factory=create_client,
    )

    async def exercise() -> dict[str, dict[str, object]]:
        await manager.start(_registry())
        await manager.close()
        return {state["id"]: dict(state) for state in manager.snapshot()}

    snapshot = asyncio.run(exercise())

    assert closed == ["broken-close", "working-close"]
    assert snapshot["working-close"]["error_code"] == "mcp_client_closed"
    assert snapshot["broken-close"]["error_code"] == "mcp_close_failed"
    assert "secret-close-detail" not in repr(snapshot)


def test_cancelled_close_finishes_cleanup_and_can_restart() -> None:
    tool = _mcp_tool("cancel-close")
    registry = _registry()
    registry.register(tool)

    async def exercise() -> dict[str, object]:
        close_entered = asyncio.Event()
        allow_close = asyncio.Event()
        attempts = 0
        closes = 0

        @asynccontextmanager
        async def client_context() -> AsyncIterator[Client]:
            nonlocal attempts, closes
            attempts += 1
            try:
                yield cast(Client, object())
            finally:
                close_entered.set()
                await allow_close.wait()
                closes += 1

        manager = _manager(
            [_server("cancel-close")],
            client_factory=lambda config: client_context(),
        )
        await manager.start(registry)

        close_task = asyncio.create_task(manager.close())
        await close_entered.wait()
        close_task.cancel()
        await asyncio.sleep(0)
        pending_after_cancel = not close_task.done()
        during_cancel = dict(manager.snapshot()[0])
        available_during_cancel = tool.available
        allow_close.set()
        with pytest.raises(asyncio.CancelledError):
            await close_task

        after_cancel = dict(manager.snapshot()[0])
        closes_after_cancel = closes
        await manager.close()
        after_retry_close = dict(manager.snapshot()[0])
        await manager.start(registry)
        after_restart = dict(manager.snapshot()[0])
        available_after_restart = tool.available
        await manager.close()
        return {
            "pending_after_cancel": pending_after_cancel,
            "during_cancel": during_cancel,
            "available_during_cancel": available_during_cancel,
            "after_cancel": after_cancel,
            "closes_after_cancel": closes_after_cancel,
            "after_retry_close": after_retry_close,
            "after_restart": after_restart,
            "available_after_restart": available_after_restart,
            "attempts": attempts,
            "closes": closes,
        }

    result = asyncio.run(exercise())

    assert result["pending_after_cancel"] is True
    assert result["during_cancel"] == {
        "id": "cancel-close",
        "transport": "stdio",
        "status": "unavailable",
        "error_code": "mcp_client_closing",
        "message": "server connection is closing",
    }
    assert result["available_during_cancel"] is False
    assert result["closes_after_cancel"] == 1
    assert result["after_cancel"] == {
        "id": "cancel-close",
        "transport": "stdio",
        "status": "unavailable",
        "error_code": "mcp_close_cancelled",
        "message": "server connection close was cancelled",
    }
    assert result["after_retry_close"] == result["after_cancel"]
    assert result["after_restart"]["status"] == "available"
    assert result["available_after_restart"] is True
    assert result["attempts"] == 2
    assert result["closes"] == 2
    assert tool.available is False


def test_started_manager_rejects_a_different_registry() -> None:
    @asynccontextmanager
    async def client_context() -> AsyncIterator[Client]:
        yield cast(Client, object())

    manager = _manager(
        [_server("bound")],
        client_factory=lambda config: client_context(),
    )

    async def exercise() -> None:
        await manager.start(_registry())
        with pytest.raises(RuntimeError, match="another tool registry"):
            await manager.start(_registry())
        await manager.close()

    asyncio.run(exercise())
