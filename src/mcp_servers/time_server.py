"""Local time MCP server implemented with the official SDK."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent

INVALID_TIME_ZONE_MESSAGE = "Unknown IANA time zone"

server = MCPServer(
    "ragrag-local-time",
    instructions="Return the current time for an IANA time zone.",
)


def _error_result(message: str) -> CallToolResult:
    """Build a stable standard MCP tool error without exposing user input."""
    return CallToolResult(
        content=[TextContent(type="text", text=message)],
        isError=True,
    )


def _format_utc_offset(offset: timedelta) -> str:
    """Format a UTC offset as a signed hours-and-minutes string."""
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    hours, minutes = divmod(abs(total_minutes), 60)
    return f"{sign}{hours:02d}:{minutes:02d}"


@server.tool(structured_output=False)
def get_current_time(timezone: str = "UTC") -> CallToolResult:
    """Return the current local time for an IANA time zone."""
    try:
        zone = ZoneInfo(timezone)
    except (ValueError, ZoneInfoNotFoundError):
        return _error_result(INVALID_TIME_ZONE_MESSAGE)

    current = datetime.now(zone)
    offset = current.utcoffset()
    if offset is None:
        return _error_result("Time zone UTC offset is unavailable")

    payload = {
        "timezone": zone.key,
        "local_time": current.isoformat(timespec="seconds"),
        "utc_offset": _format_utc_offset(offset),
        "abbreviation": current.tzname() or zone.key,
    }
    return CallToolResult(
        content=[
            TextContent(
                type="text",
                text=(
                    f"{payload['local_time']} "
                    f"({payload['timezone']}, UTC{payload['utc_offset']})"
                ),
            )
        ],
        structuredContent=payload,
    )


def main() -> None:
    """Run the local example over stdio until the host closes the session."""
    server.run("stdio")


if __name__ == "__main__":
    main()
