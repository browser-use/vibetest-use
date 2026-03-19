"""Backward-compatibility shim — all logic now lives in blop."""

__version__ = "0.2.0"

# Re-export the MCP server entry point so existing consumers still work
try:
    from blop.server import run  # noqa: F401
except ImportError:
    pass
