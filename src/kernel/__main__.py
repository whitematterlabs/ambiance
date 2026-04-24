"""Kernel entrypoint — `python -m kernel run` starts the async kernel loop.

Everything else (spawn/ls/status/stop/resolve) lives in `bin/paictl`. This
module is intentionally thin; it exists so `pai.py` can supervise the kernel
as a subprocess via a stable `python -m kernel run` invocation.
"""

import asyncio
import sys

from . import main as kernel_main


def main() -> int:
    args = sys.argv[1:]
    if args and args[0] != "run":
        print(f"unknown command: {args[0]!r}. usage: python -m kernel run", file=sys.stderr)
        return 2
    try:
        asyncio.run(kernel_main.run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
