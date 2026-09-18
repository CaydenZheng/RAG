"""MCP schemas remain complete while execution stays bounded and auditable."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

import pytest
from mcp import Client
from mcp.types import ListToolsResult, Tool

if TYPE_CHECKING:
    from config.settings import MCPStdioServerConfig
    from src.agent.mcp_client import MCPClientManager
    from src.agent.tools import ToolRegistry
    from src.core.agent_runtime import ToolResult


def _server() -> MCPStdioServerConfig:
    from config.settings import MCPStdioServerConfig

    return MCPStdioServerConfig(
        id="schema",
        transport="stdio",
        command="unused-in-unit-tests",
    )


def _registry() -> ToolRegistry:
    from src.agent.tools import ToolRegistry

    return ToolRegistry(dedup_window=0)


def _manager(client: Client) -> MCPClientManager:
    from src.agent.mcp_client import MCPClientManager

    @asynccontextmanager
    async def client_context() -> AsyncIterator[Client]:
        yield client

    return MCPClientManager(
        [_server()],
        client_factory=lambda config: client_context(),
    )


def test_complex_schema_is_preserved_and_rejected_before_sdk_call(
    isolated_runtime: Path,
) -> None:
    schema = {
        "type": "object",
        "properties": {
            "profile": {
                "type": "object",
                "properties": {
                    "tags": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                    }
                },
                "required": ["tags"],
                "additionalProperties": False,
            },
            "selector": {
                "oneOf": [
                    {"type": "string"},
                    {"type": "array", "items": {"type": "integer"}},
                ]
            },
        },
        "required": ["profile"],
        "additionalProperties": False,
    }
    client = AsyncMock(spec=Client)
    client.list_tools.return_value = ListToolsResult(
        tools=[Tool(name="complex", description="complex", inputSchema=schema)]
    )
    manager = _manager(cast(Client, client))
    registry = _registry()

    async def exercise() -> tuple[object, object]:
        await manager.start(registry)
        tool = registry.get_tool("mcp__schema__complex")
        assert tool is not None
        result = await registry.execute_async(
            tool.name,
            {"profile": {"tags": ["do-not-log", 1]}},
            session_id="invalid-schema",
        )
        await manager.close()
        return tool, result

    tool, result = asyncio.run(exercise())

    assert tool.input_schema == schema
    assert tool.input_schema is not schema
    assert [param.name for param in tool.params] == ["profile", "selector"]
    assert any("selector" in warning for warning in tool.schema_warnings)
    assert result.error_code == "invalid_tool_parameters"
    assert "do-not-log" not in result.error
    client.call_tool.assert_not_awaited()

    audit_path = isolated_runtime / "logs" / "audit.jsonl"
    audit_text = audit_path.read_text(encoding="utf-8")
    record = json.loads(audit_text)
    assert record["tool_name"] == "mcp__schema__complex"
    assert record["parameter_names"] == ["profile"]
    assert record["error_code"] == "invalid_tool_parameters"
    assert "do-not-log" not in audit_text


def test_sync_validation_failure_audits_mixed_parameter_keys(
    isolated_runtime: Path,
) -> None:
    schema = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
        "additionalProperties": False,
    }
    client = AsyncMock(spec=Client)
    client.list_tools.return_value = ListToolsResult(
        tools=[Tool(name="mixed", inputSchema=schema)]
    )
    manager = _manager(cast(Client, client))
    registry = _registry()

    async def exercise() -> ToolResult:
        await manager.start(registry)
        result = registry.execute(
            "mcp__schema__mixed",
            {"value": "ok", 1: "do-not-log"},
            session_id="mixed-sync",
        )
        await manager.close()
        return result

    result = asyncio.run(exercise())

    assert result.error_code == "invalid_tool_parameters"
    client.call_tool.assert_not_awaited()
    audit_text = (isolated_runtime / "logs" / "audit.jsonl").read_text(
        encoding="utf-8"
    )
    record = json.loads(audit_text)
    assert record["parameter_names"] == ["<non-string>", "value"]
    assert record["error_code"] == "invalid_tool_parameters"
    assert "ok" not in audit_text
    assert "do-not-log" not in audit_text


def test_async_validation_failure_audits_mixed_parameter_keys(
    isolated_runtime: Path,
) -> None:
    schema = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
        "additionalProperties": False,
    }
    client = AsyncMock(spec=Client)
    client.list_tools.return_value = ListToolsResult(
        tools=[Tool(name="mixed", inputSchema=schema)]
    )
    manager = _manager(cast(Client, client))
    registry = _registry()

    async def exercise() -> ToolResult:
        await manager.start(registry)
        result = await registry.execute_async(
            "mcp__schema__mixed",
            {"value": "ok", 1: "do-not-log"},
            session_id="mixed-async",
        )
        await manager.close()
        return result

    result = asyncio.run(exercise())

    assert result.error_code == "invalid_tool_parameters"
    client.call_tool.assert_not_awaited()
    audit_text = (isolated_runtime / "logs" / "audit.jsonl").read_text(
        encoding="utf-8"
    )
    record = json.loads(audit_text)
    assert record["parameter_names"] == ["<non-string>", "value"]
    assert record["error_code"] == "invalid_tool_parameters"
    assert "ok" not in audit_text
    assert "do-not-log" not in audit_text


def test_unsafe_discovered_schema_logs_only_a_stable_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.agent import mcp_client
    from src.agent.tool_schema import MAX_TOOL_SCHEMA_BYTES

    client = AsyncMock(spec=Client)
    client.list_tools.return_value = ListToolsResult(
        tools=[
            Tool(
                name="unsafe",
                inputSchema={
                    "type": "object",
                    "description": "do-not-log" * MAX_TOOL_SCHEMA_BYTES,
                },
            )
        ]
    )
    warnings: list[tuple[str, tuple[object, ...]]] = []
    monkeypatch.setattr(
        mcp_client.logger,
        "warning",
        lambda message, *args: warnings.append((message, args)),
    )
    manager = _manager(cast(Client, client))

    async def exercise() -> dict[str, object]:
        registry = _registry()
        await manager.start(registry)
        snapshot = dict(manager.snapshot()[0])
        await manager.close()
        assert registry.get_tool("mcp__schema__unsafe") is None
        return snapshot

    snapshot = asyncio.run(exercise())

    assert snapshot["status"] == "degraded"
    assert snapshot["error_code"] == "mcp_tool_definition_rejected"
    assert any("size limit" in repr(args) for _, args in warnings)
    assert "do-not-log" not in repr(warnings)
