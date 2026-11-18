"""The native ``transitous`` frontend: parser, conversion, output and rendering.

Importing this package or any of its modules has no side effect on standard
streams, the network or a process.  Nothing here imports ``server`` or
:mod:`transitous_mcp`: the CLI talks to :mod:`transit_core.backend` directly.
"""

__all__ = ["cli", "declaration", "human"]
