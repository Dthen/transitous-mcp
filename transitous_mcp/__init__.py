"""Namespaced home of the Transitous MCP adapter.

Importing this package has no side effect.  The adapter lives in
:mod:`transitous_mcp.server`; the root ``server.py`` launcher re-exports it for
existing source checkouts, and ``python -m transitous_mcp`` is the installed
module form.
"""

__all__ = ["server"]
