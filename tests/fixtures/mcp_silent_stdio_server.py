"""Silent stdio peer used to verify bounded application startup."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _write_marker(environment_name: str, value: str) -> None:
    """Write an optional lifecycle marker for the parent test."""
    marker = os.environ.get(environment_name)
    if marker:
        Path(marker).write_text(value, encoding="utf-8")


def main() -> None:
    """Consume client input without ever replying to the MCP handshake."""
    _write_marker("MCP_TEST_STARTED_MARKER", "started")
    try:
        while sys.stdin.buffer.read(1):
            pass
    finally:
        _write_marker("MCP_TEST_EXIT_MARKER", "closed")


if __name__ == "__main__":
    main()
