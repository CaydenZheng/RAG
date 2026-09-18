"""Launch the product time server and record clean stdio shutdown for tests."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> None:
    """Import the product module from the repository root and run it."""
    project_root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(project_root))

    from src.mcp_servers.time_server import main as run_server

    exit_marker = os.environ.get("MCP_TEST_EXIT_MARKER")
    try:
        run_server()
    finally:
        if exit_marker:
            Path(exit_marker).write_text("closed", encoding="utf-8")


if __name__ == "__main__":
    main()
