"""Boot entrypoint, executed by /sbin/init via execvp.

Runs phases 1–6 synchronously, then enters phase 7 (the asyncio
supervise loop) by delegating to boot.main.run().
"""
from __future__ import annotations

import asyncio
import sys
import traceback

from .phases import clean, probe, reconcile, sanity, start
from . import main as supervise


def boot() -> int:
    try:
        sanity.run()
        clean.run()
        probe.run()
        reconcile.run()
        start.run()
    except sanity.SanityError as e:
        print(f"[boot] sanity failed: {e}", file=sys.stderr, flush=True)
        return 1
    except Exception as e:
        tb = traceback.format_exc()
        print(f"[boot] phase failed: {e!r}\n{tb}", file=sys.stderr, flush=True)
        return 2
    try:
        asyncio.run(supervise.run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(boot())
