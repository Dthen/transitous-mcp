"""``python -m transitous_cli`` — the module form of the installed executable."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
