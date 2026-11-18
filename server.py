#!/usr/bin/env python3
"""Compatibility launcher for the Transitous MCP adapter.

The adapter itself lives in :mod:`transitous_mcp.server` (the installed package
home).  This root file keeps every existing source checkout entry point working
without an installation or configuration change:

* ``/usr/bin/python3 /path/to/repo/server.py``
* ``import server`` with its historical public names
* ``runpy.run_path("server.py", run_name="__main__")``

It is a re-export, not a second protocol implementation: the registry, era
envelope and JSON-RPC loop have exactly one definition.  Importing it has no
side effect; the stdio loop starts only under ``__main__``.
"""

from transitous_mcp.server import (  # noqa: F401  (public compatibility names)
    ERA_RESULT_FIELDS,
    ERA_VERSION,
    GEOCODE_MODES,
    MATCH_TYPES,
    OPERATION_BY_TOOL,
    RESULT_META,
    SERVER_INFO,
    TOOLS,
    TRANSIT_MODES,
    era_result,
    handle_call,
    main,
    send,
)

if __name__ == "__main__":
    main()
