"""reboot — restart the kernel in place.

Emits a `kernel:restart` event. The kernel drains in-flight nudges,
gracefully stops driver subprocesses, then `os.execvp`s itself with the
same argv `/sbin/init` uses. PID 1 is preserved across the exec.
"""

from __future__ import annotations

import sys

from boot import processes as P


def main(argv: list[str] | None = None) -> int:
    P.emit_event({"kind": "kernel:restart", "source": "reboot"})
    print("kernel:restart emitted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
