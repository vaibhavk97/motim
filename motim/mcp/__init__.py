"""MOTIM MCP server package.

Exposes motim's primitives as a Model Context Protocol server so any
MCP-compatible agent can drive motim without shelling out to the CLI.

Entry points:
- ``motim mcp`` (Click command)
- ``python -m motim.mcp`` (module entry)
- ``from motim.mcp.server import build_server`` (in-process embedding/tests)

Requires the optional dependency: ``pip install 'motim[mcp]'``.
"""

from __future__ import annotations

__all__ = ["build_server", "main"]


def __getattr__(name: str):  # pragma: no cover - lazy import shim
    # Lazy import so importing this package does not require ``mcp`` to be
    # installed unless the server is actually used.
    if name in __all__:
        from .server import build_server, main

        return {"build_server": build_server, "main": main}[name]
    raise AttributeError(name)
