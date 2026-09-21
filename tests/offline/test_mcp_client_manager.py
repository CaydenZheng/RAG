"""MCP Client Manager owns optional server lifecycles without cross-failure."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, call

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


def _client_with_tools(*tool_names: str) -> Client:
    from mcp.types import ListToolsResult, Tool

    client = AsyncMock(spec=Client)
    client.list_tools.return_value = ListToolsResult(
        tools=[
            Tool(
                name=name,
                inputSchema={"type": "object", "properties": {}},
            )
            for name in tool_names
        ]
    )
    return cast(Client, client)


def _empty_client() -> Client:
    return _client_with_tools()


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
        yield _empty_client()

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
            yield _empty_client()

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
        second_entered = asyncio.Event()
        release_first = asyncio.Event()
        release_second = asyncio.Event()

        @asynccontextmanager
        async def client_context(config: MCPServerConfig) -> AsyncIterator[Client]:
            if config.id == "first":
                first_entered.set()
                await release_first.wait()
            else:
                second_entered.set()
                await release_second.wait()
            yield _empty_client()

        manager = _manager(
            [_server("first"), _server("second")],
            client_factory=client_context,
        )
        start_task = asyncio.create_task(manager.start(registry))
        await first_entered.wait()
        await second_entered.wait()
        availability = (first_tool.available, second_tool.available)
        second_state = dict(manager.snapshot()[1])
        release_first.set()
        release_second.set()
        await start_task
        await manager.close()
        return *availability, second_state

    first_available, second_available, second_state = asyncio.run(exercise())

    assert first_available is False
    assert second_available is False
    assert second_state["status"] == "connecting"
    assert second_state["error_code"] is None


def test_pending_first_connection_does_not_block_later_provider() -> None:
    registry = _registry()

    async def exercise() -> tuple[dict[str, object], bool]:
        first_entered = asyncio.Event()
        release_first = asyncio.Event()

        @asynccontextmanager
        async def client_context(
            config: MCPServerConfig,
        ) -> AsyncIterator[Client]:
            if config.id == "oauth-pending":
                first_entered.set()
                await release_first.wait()
            yield _client_with_tools("probe")

        manager = _manager(
            [_server("oauth-pending"), _server("healthy")],
            client_factory=client_context,
        )
        start_task = asyncio.create_task(manager.start(registry))
        await first_entered.wait()
        for _ in range(20):
            healthy = manager.snapshot()[1]
            if healthy["status"] == "available":
                break
            await asyncio.sleep(0)
        else:
            raise AssertionError("later provider did not become available")

        healthy_tool = registry.get_tool("mcp__healthy__probe")
        assert healthy_tool is not None
        snapshot = dict(manager.snapshot()[1])
        available = healthy_tool.available
        release_first.set()
        await start_task
        await manager.close()
        return snapshot, available

    snapshot, available = asyncio.run(exercise())

    assert snapshot["status"] == "available"
    assert snapshot["error_code"] is None
    assert available is True


def test_one_connection_failure_does_not_block_other_servers() -> None:
    registry = _registry()
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
                yield _client_with_tools("probe")
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
        tuple[bool, bool, bool],
    ]:
        await manager.start(registry)
        started = {state["id"]: dict(state) for state in manager.snapshot()}
        working_tool = registry.get_tool("mcp__working__probe")
        broken_tool = registry.get_tool("mcp__broken__probe")
        second_tool = registry.get_tool("mcp__second__probe")
        assert working_tool is not None
        assert second_tool is not None
        availability = (
            working_tool.available,
            broken_tool is not None and broken_tool.available,
            second_tool.available,
        )
        await manager.close()
        closed = {state["id"]: dict(state) for state in manager.snapshot()}
        closed_availability = (
            working_tool.available,
            broken_tool is not None and broken_tool.available,
            second_tool.available,
        )
        return started, availability, closed, closed_availability

    started, availability, closed, closed_availability = asyncio.run(
        exercise()
    )

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
    assert closed_availability == (False, False, False)
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
                yield _empty_client()
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
    registry = _registry()

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
                yield _client_with_tools("probe")
            finally:
                close_entered.set()
                await allow_close.wait()
                closes += 1

        manager = _manager(
            [_server("cancel-close")],
            client_factory=lambda config: client_context(),
        )
        await manager.start(registry)
        tool = registry.get_tool("mcp__cancel-close__probe")
        assert tool is not None

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
        restarted_tool = registry.get_tool("mcp__cancel-close__probe")
        assert restarted_tool is not None
        replaced_on_restart = restarted_tool is not tool
        available_after_restart = restarted_tool.available
        await manager.close()
        return {
            "pending_after_cancel": pending_after_cancel,
            "during_cancel": during_cancel,
            "available_during_cancel": available_during_cancel,
            "after_cancel": after_cancel,
            "closes_after_cancel": closes_after_cancel,
            "after_retry_close": after_retry_close,
            "after_restart": after_restart,
            "replaced_on_restart": replaced_on_restart,
            "available_after_restart": available_after_restart,
            "available_after_final_close": restarted_tool.available,
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
    assert result["replaced_on_restart"] is True
    assert result["available_after_restart"] is True
    assert result["available_after_final_close"] is False
    assert result["attempts"] == 2
    assert result["closes"] == 2


def test_started_manager_rejects_a_different_registry() -> None:
    @asynccontextmanager
    async def client_context() -> AsyncIterator[Client]:
        yield _empty_client()

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


def test_dynamic_discovery_registers_namespaced_tools_and_can_restart() -> None:
    from src.agent.tools import SafetyLevel, ToolDef
    from src.core.agent_runtime import ToolResult

    first_server = MCPServer("first-discovery")

    @first_server.tool(name="lookup", description="Look up from the first server")
    def first_lookup(
        query: str,
        filters: dict[str, list[str]] | None = None,
    ) -> dict[str, str]:
        return {
            "server": "first",
            "query": query,
            "filter_count": str(len(filters or {})),
        }

    second_server = MCPServer("second-discovery")

    @second_server.tool(name="lookup", description="Look up from the second server")
    def second_lookup(query: str) -> dict[str, str]:
        return {"server": "second", "query": query}

    servers: dict[str, MCPServer] = {
        "first": first_server,
        "second": second_server,
    }
    registry = _registry()
    registry.register(
        ToolDef(
            name="lookup",
            description="native lookup",
            params=[],
            safety_level=SafetyLevel.WHITELIST,
            execute_fn=lambda params: ToolResult(success=True),
        )
    )

    def create_client(config: MCPServerConfig) -> Client:
        return Client(servers[config.id])

    manager = _manager(
        [_server("first"), _server("second")],
        client_factory=create_client,
    )

    async def exercise() -> tuple[ToolDef, ToolDef, ToolResult, bool, bool]:
        await manager.start(registry)
        first = registry.get_tool("mcp__first__lookup")
        second = registry.get_tool("mcp__second__lookup")
        assert first is not None
        assert second is not None
        result = await registry.execute_async(
            first.name,
            {"query": "needle", "filters": {"tag": ["one"]}},
            session_id="mcp-discovery",
        )
        await manager.close()
        first_closed = first.available
        second_closed = second.available
        await manager.start(registry)
        restarted = registry.get_tool("mcp__first__lookup")
        assert restarted is not None
        assert restarted.available is True
        await manager.close()
        return first, second, result, first_closed, second_closed

    first, second, result, first_closed, second_closed = asyncio.run(exercise())

    assert registry.get_tool("lookup") is not None
    assert first.name != second.name
    assert first.source == "mcp"
    assert first.provider == "first"
    assert first.category == "mcp"
    assert first.safety_level is SafetyLevel.GRAYLIST
    assert first.max_retries == 0
    assert [param.name for param in first.params] == ["query", "filters"]
    assert first.input_schema is not None
    assert first.input_schema["properties"]["query"]["type"] == "string"
    assert "array" in repr(first.input_schema["properties"]["filters"])
    assert result.success is True
    assert result.data == {
        "server": "first",
        "query": "needle",
        "filter_count": "1",
    }
    assert first_closed is False
    assert second_closed is False


def test_tool_discovery_uses_all_official_sdk_pages() -> None:
    from mcp.types import ListToolsResult, Tool

    client = AsyncMock(spec=Client)
    client.list_tools.side_effect = [
        ListToolsResult(
            tools=[
                Tool(
                    name="first",
                    description="first page",
                    inputSchema={"type": "object", "properties": {}},
                )
            ],
            nextCursor="page-two",
        ),
        ListToolsResult(
            tools=[
                Tool(
                    name="second",
                    description="second page",
                    inputSchema={"type": "object", "properties": {}},
                )
            ]
        ),
    ]

    @asynccontextmanager
    async def client_context() -> AsyncIterator[Client]:
        yield cast(Client, client)

    registry = _registry()
    manager = _manager(
        [_server("paged")],
        client_factory=lambda config: client_context(),
    )

    async def exercise() -> None:
        await manager.start(registry)
        await manager.close()

    asyncio.run(exercise())

    assert client.list_tools.await_args_list == [
        call(cursor=None, cache_mode="refresh"),
        call(cursor="page-two", cache_mode="refresh"),
    ]
    assert registry.get_tool("mcp__paged__first") is not None
    assert registry.get_tool("mcp__paged__second") is not None


def test_discovery_failure_isolated_from_other_servers_and_native_tools() -> None:
    from mcp.types import ListToolsResult, Tool

    from src.agent.tools import SafetyLevel, ToolDef
    from src.core.agent_runtime import ToolResult

    working_client = AsyncMock(spec=Client)
    working_client.list_tools.return_value = ListToolsResult(
        tools=[
            Tool(
                name="healthy",
                description="healthy tool",
                inputSchema={"type": "object", "properties": {}},
            )
        ]
    )
    broken_client = AsyncMock(spec=Client)
    broken_client.list_tools.side_effect = RuntimeError(
        "secret-discovery-detail"
    )
    clients: dict[str, AsyncMock] = {
        "working": working_client,
        "broken": broken_client,
    }

    @asynccontextmanager
    async def client_context(
        config: MCPServerConfig,
    ) -> AsyncIterator[Client]:
        yield cast(Client, clients[config.id])

    registry = _registry()
    native = ToolDef(
        name="native",
        description="native tool",
        params=[],
        safety_level=SafetyLevel.WHITELIST,
        execute_fn=lambda params: ToolResult(success=True),
    )
    registry.register(native)
    manager = _manager(
        [_server("broken"), _server("working")],
        client_factory=client_context,
    )

    async def exercise() -> dict[str, dict[str, object]]:
        await manager.start(registry)
        snapshot = {
            state["id"]: dict(state) for state in manager.snapshot()
        }
        await manager.close()
        return snapshot

    snapshot = asyncio.run(exercise())

    assert snapshot["broken"]["status"] == "unavailable"
    assert snapshot["broken"]["error_code"] == "mcp_tool_discovery_failed"
    assert snapshot["working"]["status"] == "available"
    assert registry.get_tool("mcp__working__healthy") is not None
    assert native.available is True
    assert "secret-discovery-detail" not in repr(snapshot)


def test_invalid_or_colliding_tool_definition_degrades_only_its_server() -> None:
    from mcp.types import ListToolsResult, Tool

    from src.agent.tools import SafetyLevel, ToolDef
    from src.core.agent_runtime import ToolResult

    client = AsyncMock(spec=Client)
    client.list_tools.return_value = ListToolsResult(
        tools=[
            Tool(
                name="safe",
                description="safe tool",
                inputSchema={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                },
            ),
            Tool(
                name="unsafe",
                description="secret-schema-description",
                inputSchema={"$ref": "#"},
            ),
            Tool(
                name="collision",
                description="must not replace native",
                inputSchema={"type": "object", "properties": {}},
            ),
        ]
    )

    @asynccontextmanager
    async def client_context() -> AsyncIterator[Client]:
        yield cast(Client, client)

    registry = _registry()
    native_collision = ToolDef(
        name="mcp__mixed__collision",
        description="native collision",
        params=[],
        safety_level=SafetyLevel.WHITELIST,
        execute_fn=lambda params: ToolResult(success=True),
    )
    registry.register(native_collision)
    manager = _manager(
        [_server("mixed")],
        client_factory=lambda config: client_context(),
    )

    async def exercise() -> tuple[dict[str, object], bool]:
        await manager.start(registry)
        snapshot = dict(manager.snapshot()[0])
        safe = registry.get_tool("mcp__mixed__safe")
        assert safe is not None
        safe_available = safe.available
        await manager.close()
        return snapshot, safe_available

    snapshot, safe_available = asyncio.run(exercise())

    assert snapshot["status"] == "degraded"
    assert snapshot["error_code"] == "mcp_tool_definition_rejected"
    assert safe_available is True
    assert registry.get_tool("mcp__mixed__unsafe") is None
    assert registry.get_tool("mcp__mixed__collision") is native_collision
    assert native_collision.available is True
    assert "secret-schema-description" not in repr(snapshot)


def test_cancelled_tool_discovery_converges_state_and_can_restart() -> None:
    from mcp.types import ListToolsResult

    async def exercise() -> tuple[dict[str, object], dict[str, object]]:
        entered = asyncio.Event()
        release = asyncio.Event()
        attempts = 0
        client = AsyncMock(spec=Client)

        async def list_tools(**kwargs: object) -> ListToolsResult:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                entered.set()
                await release.wait()
            return ListToolsResult(tools=[])

        client.list_tools.side_effect = list_tools

        @asynccontextmanager
        async def client_context() -> AsyncIterator[Client]:
            yield cast(Client, client)

        manager = _manager(
            [_server("cancel-discovery")],
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


def test_tool_discovery_stops_at_the_page_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp.types import ListToolsResult

    from src.agent import mcp_client

    monkeypatch.setattr(mcp_client, "MAX_MCP_TOOL_PAGES", 2)
    client = AsyncMock(spec=Client)
    client.list_tools.side_effect = [
        ListToolsResult(tools=[], nextCursor="page-two"),
        ListToolsResult(tools=[], nextCursor="page-three"),
    ]

    @asynccontextmanager
    async def client_context() -> AsyncIterator[Client]:
        yield cast(Client, client)

    manager = _manager(
        [_server("unbounded")],
        client_factory=lambda config: client_context(),
    )

    async def exercise() -> dict[str, object]:
        await manager.start(_registry())
        snapshot = dict(manager.snapshot()[0])
        await manager.close()
        return snapshot

    snapshot = asyncio.run(exercise())

    assert client.list_tools.await_count == 2
    assert snapshot["status"] == "unavailable"
    assert snapshot["error_code"] == "mcp_tool_discovery_failed"


def test_new_manager_replaces_tools_bound_to_a_closed_client() -> None:
    first_server = MCPServer("first-owner")

    @first_server.tool(name="identity")
    def first_identity() -> dict[str, str]:
        return {"owner": "first"}

    second_server = MCPServer("second-owner")

    @second_server.tool(name="identity")
    def second_identity() -> dict[str, str]:
        return {"owner": "second"}

    registry = _registry()

    async def exercise() -> tuple[ToolDef, ToolDef, dict[str, object]]:
        first_manager = _manager(
            [_server("shared")],
            client_factory=lambda config: Client(first_server),
        )
        await first_manager.start(registry)
        first_tool = registry.get_tool("mcp__shared__identity")
        assert first_tool is not None
        await first_manager.close()

        second_manager = _manager(
            [_server("shared")],
            client_factory=lambda config: Client(second_server),
        )
        await second_manager.start(registry)
        second_tool = registry.get_tool("mcp__shared__identity")
        assert second_tool is not None
        result = await registry.execute_async(
            second_tool.name,
            {},
            session_id="replacement-manager",
        )
        snapshot = dict(second_manager.snapshot()[0])
        await second_manager.close()
        assert result.success is True
        assert result.data == {"owner": "second"}
        return first_tool, second_tool, snapshot

    first_tool, second_tool, snapshot = asyncio.run(exercise())

    assert second_tool is not first_tool
    assert first_tool.available is False
    assert second_tool.available is False
    assert snapshot["status"] == "available"
    assert snapshot["error_code"] is None


def test_tool_discovery_rejects_more_than_one_thousand_tools() -> None:
    from mcp.types import ListToolsResult, Tool

    client = AsyncMock(spec=Client)
    client.list_tools.return_value = ListToolsResult(
        tools=[
            Tool(
                name=f"tool-{index}",
                inputSchema={"type": "object", "properties": {}},
            )
            for index in range(1001)
        ]
    )

    @asynccontextmanager
    async def client_context() -> AsyncIterator[Client]:
        yield cast(Client, client)

    registry = _registry()
    manager = _manager(
        [_server("too-many-tools")],
        client_factory=lambda config: client_context(),
    )

    async def exercise() -> dict[str, object]:
        await manager.start(registry)
        snapshot = dict(manager.snapshot()[0])
        await manager.close()
        return snapshot

    snapshot = asyncio.run(exercise())

    assert snapshot["status"] == "unavailable"
    assert snapshot["error_code"] == "mcp_tool_discovery_failed"
    assert registry.tools == {}


def test_tool_discovery_rejects_a_repeated_cursor() -> None:
    from mcp.types import ListToolsResult

    client = AsyncMock(spec=Client)
    client.list_tools.side_effect = [
        ListToolsResult(tools=[], nextCursor="repeated"),
        ListToolsResult(tools=[], nextCursor="repeated"),
    ]

    @asynccontextmanager
    async def client_context() -> AsyncIterator[Client]:
        yield cast(Client, client)

    manager = _manager(
        [_server("cursor-cycle")],
        client_factory=lambda config: client_context(),
    )

    async def exercise() -> dict[str, object]:
        await manager.start(_registry())
        snapshot = dict(manager.snapshot()[0])
        await manager.close()
        return snapshot

    snapshot = asyncio.run(exercise())

    assert client.list_tools.await_count == 2
    assert snapshot["status"] == "unavailable"
    assert snapshot["error_code"] == "mcp_tool_discovery_failed"
