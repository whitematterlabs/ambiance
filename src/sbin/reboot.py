"""reboot — restart the kernel in place.

Verifies a kernel is actually running (by trying to acquire its flock —
if we can, no kernel is up to receive the event), then emits
`kernel:restart`. The kernel drains in-flight nudges, gracefully stops
driver subprocesses, then `os.execvp`s itself with the same argv
`/sbin/init` uses. PID is preserved across the exec.
"""

from __future__ import annotations

import fcntl
import os
import sys

from boot import paths, processes as P

_LOCK_FILE = paths.PAI_ROOT / "run" / "kernel.pid"


def _kernel_is_running() -> tuple[bool, str | None]:
    """Return (running, pid_string). Probes by attempting a non-blocking
    flock on the kernel's lock file: if we can grab it, no kernel holds
    it. We always release immediately."""
    if not _LOCK_FILE.exists():
        return (False, None)
    try:
        fd = os.open(_LOCK_FILE, os.O_RDWR)
    except OSError:
        return (False, None)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            try:
                pid = os.read(fd, 64).decode().strip() or None
            except OSError:
                pid = None
            return (True, pid)
        # Got the lock — no kernel is running. Release and report.
        fcntl.flock(fd, fcntl.LOCK_UN)
        return (False, None)
    finally:
        os.close(fd)


def main(argv: list[str] | None = None) -> int:
    running, pid = _kernel_is_running()
    if not running:
        print(
            "reboot: no kernel is running — nothing to restart.\n"
            "       start one with `init` (or `cd ~/.pai && usr/bin/python -m boot.entry`).",
            file=sys.stderr,
        )
        return 1
    P.emit_event({"kind": "kernel:restart", "source": "reboot"})
    print(f"kernel:restart emitted (kernel pid={pid})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
