"""Real SDK server used by the Streamable HTTP transport integration test."""

from __future__ import annotations

import asyncio
import os

from mcp.server import MCPServer

server = MCPServer("streamable-http-transport-test")


@server.tool()
def echo(value: str) -> str:
    """Return the supplied value over the real MCP HTTP connection."""
    return value


@server.tool()
async def wait_for(delay_seconds: float) -> str:
    """Delay a response so the real client timeout path can be exercised."""
    await asyncio.sleep(delay_seconds)
    return "completed"


def main() -> None:
    """Run the official SDK server on the test-selected loopback port."""
    port = int(os.environ["MCP_TEST_PORT"])
    server.run(
        "streamable-http",
        host="127.0.0.1",
        port=port,
        streamable_http_path="/mcp",
    )


if __name__ == "__main__":
    main()
