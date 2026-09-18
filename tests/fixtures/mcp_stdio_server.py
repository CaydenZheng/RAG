"""Real SDK server used only by the stdio transport integration test."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from mcp.server import MCPServer

server = MCPServer("stdio-transport-test")


@server.tool()
def echo(value: str) -> str:
    """Return the supplied value over the real MCP stdio connection."""
    return value


def main() -> None:
    """Run until the client closes stdin, then record clean process exit."""
    exit_marker = os.environ.get("MCP_TEST_EXIT_MARKER")
    untrusted_output = os.environ.get("MCP_TEST_UNTRUSTED_OUTPUT")
    if untrusted_output:
        print(untrusted_output, file=sys.stderr, flush=True)
        print(untrusted_output, flush=True)
    try:
        server.run("stdio")
    finally:
        if exit_marker:
            Path(exit_marker).write_text("closed", encoding="utf-8")


if __name__ == "__main__":
    main()
