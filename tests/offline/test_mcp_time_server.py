"""The bundled local time MCP server works through the real stdio path."""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import SecretStr

if TYPE_CHECKING:
    from src.agent.tools import ToolRegistry
    from src.core.agent_runtime import ToolResult


def _registry() -> ToolRegistry:
    from src.agent.tools import ToolRegistry

    return ToolRegistry(dedup_window=0)


def test_time_server_returns_stable_error_without_echoing_input() -> None:
    from src.mcp_servers.time_server import (
        INVALID_TIME_ZONE_MESSAGE,
        get_current_time,
    )

    invalid_timezone = "invalid-zone-do-not-echo"
    result = get_current_time(invalid_timezone)

    assert result.is_error is True
    assert result.structured_content is None
    assert len(result.content) == 1
    assert result.content[0].text == INVALID_TIME_ZONE_MESSAGE
    assert invalid_timezone not in repr(result)


def test_time_server_runs_through_manager_and_closes_cleanly(
    isolated_runtime: Path,
) -> None:
    from config.settings import MCPStdioServerConfig
    from src.agent.mcp_client import MCPClientManager

    project_root = Path(__file__).resolve().parents[2]
    launcher = project_root / "tests" / "fixtures" / "mcp_time_server_launcher.py"
    exit_marker = isolated_runtime / "time-server-closed"
    configured = MCPStdioServerConfig(
        id="clock",
        transport="stdio",
        command=sys.executable,
        args=(str(launcher),),
        env={"MCP_TEST_EXIT_MARKER": SecretStr(str(exit_marker))},
        cwd=str(project_root),
    )
    registry = _registry()
    manager = MCPClientManager([configured])

    async def exercise() -> tuple[
        dict[str, object],
        ToolResult,
        ToolResult,
        ToolResult,
        dict[str, object],
    ]:
        await manager.start(registry)
        started = dict(manager.snapshot()[0])
        utc_result = await registry.execute_async(
            "mcp__clock__get_current_time",
            {},
            session_id="clock-utc",
        )
        shanghai_result = await registry.execute_async(
            "mcp__clock__get_current_time",
            {"timezone": "Asia/Shanghai"},
            session_id="clock-shanghai",
        )
        invalid_result = await registry.execute_async(
            "mcp__clock__get_current_time",
            {"timezone": "Not/A_Time_Zone"},
            session_id="clock-invalid",
        )
        await manager.close()
        return (
            started,
            utc_result,
            shanghai_result,
            invalid_result,
            dict(manager.snapshot()[0]),
        )

    started, utc_result, shanghai_result, invalid_result, closed = asyncio.run(
        exercise()
    )

    assert started["status"] == "available"
    assert utc_result.success is True
    assert utc_result.data["timezone"] == "UTC"
    assert utc_result.data["utc_offset"] == "+00:00"
    utc_time = datetime.fromisoformat(utc_result.data["local_time"])
    assert utc_time.utcoffset() == timedelta(0)

    assert shanghai_result.success is True
    assert shanghai_result.data["timezone"] == "Asia/Shanghai"
    assert shanghai_result.data["utc_offset"] == "+08:00"
    shanghai_time = datetime.fromisoformat(shanghai_result.data["local_time"])
    assert shanghai_time.utcoffset() == timedelta(hours=8)

    assert invalid_result.success is False
    assert invalid_result.error_code == "mcp_tool_error"
    assert invalid_result.error == "MCP tool returned an error"

    assert closed["status"] == "unavailable"
    assert closed["error_code"] == "mcp_client_closed"
    assert exit_marker.read_text(encoding="utf-8") == "closed"

    tool = registry.get_tool("mcp__clock__get_current_time")
    assert tool is not None
    assert tool.available is False
    assert tool.input_schema["properties"]["timezone"]["default"] == "UTC"
