"""Developer tooling helpers for blop."""
from __future__ import annotations

import subprocess
import sys


def launch_inspector() -> int:
    """Launch MCP Inspector pointed at the blop MCP server.

    Opens an interactive UI at http://localhost:5173 for calling blop tools
    without needing a Claude/Cursor integration.

    Requires Node.js and npx to be installed.
    """
    cmd = ["npx", "@modelcontextprotocol/inspector", "uvx", "blop-mcp"]
    print("Launching MCP Inspector — open http://localhost:5173 in your browser")
    print(f"Command: {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, check=False)
        return result.returncode
    except FileNotFoundError:
        print("npx not found. Install Node.js to use blop-inspect.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(launch_inspector())
