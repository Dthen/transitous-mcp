"""``python -m transitous_mcp`` — module form of the installed MCP executable."""

import sys

from .server import main

if __name__ == "__main__":
    sys.exit(main())
