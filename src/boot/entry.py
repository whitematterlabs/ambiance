"""Boot entrypoint, executed by /sbin/init via execvp.

Runs phases 1–6 synchronously, then enters phase 7 (the asyncio
supervise loop) by delegating to boot.main.run().
"""
from __future__ import annotations

import asyncio
import atexit
import os
import sys
import traceback

from . import paths
from .phases import clean, probe, reconcile, sanity, start
from . import main as supervise

_PID_FILE = paths.PAI_ROOT / "run" / "kernel.pid"


def _acquire_pid_lock() -> bool:
    """Write PID file; return False if another kernel is already running."""
    if _PID_FILE.exists():
        try:
            existing = int(_PID_FILE.read_text().strip())
        except (ValueError, OSError):
            existing = None
        if existing is not None and existing != os.getpid():
            try:
                os.kill(existing, 0)
                print(
                    f"[boot] kernel already running (pid={existing}); exiting",
                    file=sys.stderr,
                    flush=True,
                )
                return False
            except ProcessLookupError:
                pass  # stale file from a crashed kernel
    _PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PID_FILE.write_text(f"{os.getpid()}\n")
    atexit.register(_release_pid_lock)
    return True


def _release_pid_lock() -> None:
    try:
        _PID_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def boot() -> int:
    if not _acquire_pid_lock():
        return 1
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
    if supervise._restart_requested:
        print("[boot] re-exec for kernel:restart", flush=True)
        _release_pid_lock()  # remove before exec so the re-exec can re-acquire
        os.execvp(sys.executable, [sys.executable, "-u", "-m", "boot.entry"])
        raise AssertionError("execvp returned without replacing process")
    return 0


if __name__ == "__main__":
    sys.exit(boot())
