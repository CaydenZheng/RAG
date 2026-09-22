"""Real SDK server used only by the stdio transport integration test."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Annotated

from mcp.server import MCPServer
from mcp.server.mcpserver.resolve import Elicit, Resolve
from pydantic import BaseModel

server = MCPServer("stdio-transport-test")


class ElicitedAnswer(BaseModel):
    answer: str


def request_answer() -> Elicit[ElicitedAnswer]:
    """Ask the connected client for one form value."""
    return Elicit("Choose a stdio answer", ElicitedAnswer)


@server.tool()
def echo(value: str) -> str:
    """Return the supplied value over the real MCP stdio connection."""
    return value


@server.tool()
def ask(
    response: Annotated[ElicitedAnswer, Resolve(request_answer)],
) -> dict[str, str]:
    """Return the caller-provided Elicitation response."""
    return {"answer": response.answer}


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
