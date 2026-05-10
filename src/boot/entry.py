"""Boot entrypoint, executed by /sbin/init via execvp.

Runs phases 1–6 synchronously, then enters phase 7 (the asyncio
supervise loop) by delegating to boot.main.run().
"""
from __future__ import annotations

import asyncio
import atexit
import errno
import fcntl
import os
import subprocess
import sys
import traceback

from . import paths
from .phases import clean, hooks, probe, reconcile, sanity, start
from . import main as supervise

_LOCK_FILE = paths.PAI_ROOT / "run" / "kernel.pid"

# Held for the lifetime of the kernel process. flock() releases automatically
# on close (including SIGKILL), so we cannot leave a stale lock behind.
_lock_fd: int | None = None


def _acquire_pid_lock() -> bool:
    """Take an exclusive flock on run/kernel.pid; return False if another
    kernel already holds it. Writes our PID to the file as a human-readable
    breadcrumb (the *lock*, not the file contents, is what enforces mutex)."""
    global _lock_fd
    _LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(_LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        # The PID-file *contents* can drift from the actual lock holder
        # (e.g. a forked-then-exited parent inherits its PID into the file
        # while the child holds the lock). lsof is the source of truth for
        # who holds the fd; fall back to file contents only if that fails.
        holder = "?"
        try:
            out = subprocess.run(
                ["lsof", "-t", str(_LOCK_FILE)],
                capture_output=True,
                text=True,
                timeout=2,
            )
            pids = [p for p in out.stdout.split() if p.strip()]
            if pids:
                holder = ",".join(pids)
        except Exception:
            pass
        if holder == "?":
            try:
                holder = os.read(fd, 64).decode().strip() or "?"
            except OSError:
                pass
        os.close(fd)
        print(
            f"[boot] kernel already running (pid={holder}); exiting",
            file=sys.stderr,
            flush=True,
        )
        return False
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode())
    _lock_fd = fd
    atexit.register(_release_pid_lock)
    return True


def _release_pid_lock() -> None:
    global _lock_fd
    if _lock_fd is None:
        return
    try:
        fcntl.flock(_lock_fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(_lock_fd)
    except OSError:
        pass
    _lock_fd = None


def boot() -> int:
    if not _acquire_pid_lock():
        return 1
    try:
        sanity.run()
        clean.run()
        probe.run()
        reconcile.run()
        start.run()
        hooks.run()
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
    except BaseException:
        tb = traceback.format_exc()
        print(f"[kernel] fatal: uncaught in supervise.run()\n{tb}", file=sys.stderr, flush=True)
        try:
            from datetime import datetime
            crash_dir = paths.PAI_ROOT / "var" / "log" / "kernel"
            crash_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%dT%H%M%S")
            (crash_dir / f"crash-{ts}.log").write_text(tb)
        except Exception:
            pass
        return 3
    if supervise._restart_requested:
        print("[boot] re-exec for kernel:restart", flush=True)
        _release_pid_lock()  # drop lock so the re-exec can re-acquire
        os.execvp(sys.executable, [sys.executable, "-u", "-m", "boot.entry"])
        raise AssertionError("execvp returned without replacing process")
    return 0


if __name__ == "__main__":
    sys.exit(boot())
