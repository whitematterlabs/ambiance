"""Run the kernel and the TUI together.

Spawns the kernel as a child process (its stdout/stderr are already tee'd
to home/tmp/kernel.log by the kernel itself), then runs the TUI in the
foreground. Ctrl+C quits the TUI and terminates the kernel.

    uv run python -m pai
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys

from boot.processes import HOME_DIR
from tui.app import TuiApp


def main() -> int:
    # Point the kernel's stdout/stderr straight at kernel.log so even early
    # startup errors (import failures, missing API key) land on disk where
    # the TUI's log pane can see them. -u keeps writes unbuffered.
    log_path = HOME_DIR / "tmp" / "kernel.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("a", buffering=1, encoding="utf-8")
    log.write(f"\n--- pai supervisor starting kernel ---\n")
    log.flush()

    kernel = subprocess.Popen(
        [sys.executable, "-u", "-m", "boot", "run"],
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,  # isolate from terminal signals; we kill explicitly
    )

    exit_code = 0
    try:
        TuiApp().run()
    except KeyboardInterrupt:
        pass
    except Exception:
        exit_code = 1
        raise
    finally:
        if kernel.poll() is None:
            try:
                os.killpg(kernel.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                kernel.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(kernel.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                kernel.wait()

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
