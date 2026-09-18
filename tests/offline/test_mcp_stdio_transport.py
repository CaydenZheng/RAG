"""The default MCP factory uses the official stdio transport end to end."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pydantic import SecretStr

if TYPE_CHECKING:
    from src.agent.tools import ToolRegistry
    from src.core.agent_runtime import ToolResult


def _registry() -> ToolRegistry:
    from src.agent.tools import ToolRegistry

    return ToolRegistry(dedup_window=0)


def test_stdio_transport_discovers_calls_and_closes_real_server(
    isolated_runtime: Path,
    capfd: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    from config.settings import MCPStdioServerConfig
    from src.agent.mcp_client import MCPClientManager

    secret = "stdio-peer-do-not-log"
    caplog.set_level(logging.ERROR, logger="mcp.client.stdio")
    exit_marker = isolated_runtime / "stdio-server-closed"
    project_root = Path(__file__).resolve().parents[2]
    server_script = project_root / "tests" / "fixtures" / "mcp_stdio_server.py"
    server = MCPStdioServerConfig(
        id="stdio",
        transport="stdio",
        command=sys.executable,
        args=(str(server_script),),
        env={
            "MCP_TEST_EXIT_MARKER": SecretStr(str(exit_marker)),
            "MCP_TEST_UNTRUSTED_OUTPUT": SecretStr(secret),
        },
        cwd=str(project_root),
    )
    registry = _registry()
    manager = MCPClientManager([server])

    async def exercise() -> tuple[dict[str, object], ToolResult, dict[str, object]]:
        await manager.start(registry)
        started = dict(manager.snapshot()[0])
        result = await registry.execute_async(
            "mcp__stdio__echo",
            {"value": "round-trip"},
            session_id="stdio-integration",
        )
        await manager.close()
        return started, result, dict(manager.snapshot()[0])

    started, result, closed = asyncio.run(exercise())

    assert started["status"] == "available"
    assert result.success is True
    assert result.data == {"result": "round-trip"}
    assert closed["status"] == "unavailable"
    assert closed["error_code"] == "mcp_client_closed"
    assert exit_marker.read_text(encoding="utf-8") == "closed"
    captured = capfd.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err
    assert secret not in caplog.text
    parse_records = [
        record
        for record in caplog.records
        if record.getMessage() == "Failed to parse JSONRPC message from server"
    ]
    assert parse_records
    assert all(record.exc_info is None for record in parse_records)
    tool = registry.get_tool("mcp__stdio__echo")
    assert tool is not None
    assert tool.available is False


def test_stdio_transport_failure_is_stable_and_secret_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from config.settings import MCPStdioServerConfig
    from src.agent import mcp_client
    from src.agent.mcp_client import MCPClientManager

    secret = "stdio-do-not-log"
    warnings: list[tuple[str, tuple[object, ...]]] = []
    monkeypatch.setattr(
        mcp_client.logger,
        "warning",
        lambda message, *args: warnings.append((message, args)),
    )
    server = MCPStdioServerConfig(
        id="missing",
        transport="stdio",
        command="ragrag-missing-mcp-command",
        env={"MCP_TOKEN": SecretStr(secret)},
    )
    manager = MCPClientManager([server])

    async def exercise() -> dict[str, object]:
        await manager.start(_registry())
        snapshot = dict(manager.snapshot()[0])
        await manager.close()
        return snapshot

    snapshot = asyncio.run(exercise())

    assert snapshot["status"] == "unavailable"
    assert snapshot["error_code"] == "mcp_connect_failed"
    assert secret not in repr(snapshot)
    assert secret not in repr(warnings)
