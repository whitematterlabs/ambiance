"""pai — top-level user entrypoint.

Thin dispatcher; defers to `boot.init` (kernel) and `sbin.tui` (UI) without
modifying either.
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys

from boot.init import check_layout
from boot.paths import HOME_DIR, PAI_ROOT


def cmd_start(args: argparse.Namespace) -> int:
    missing = check_layout(PAI_ROOT)
    if missing:
        print(
            f"pai: PAI_ROOT={PAI_ROOT} missing required dirs: {', '.join(missing)}\n"
            f"     run `paifs-init` to lay out the skeleton.",
            file=sys.stderr,
        )
        return 1

    if args.headless:
        os.execvp(sys.executable, [sys.executable, "-u", "-m", "boot.entry"])
        raise AssertionError("execvp returned without replacing process")

    log_path = HOME_DIR / "tmp" / "kernel.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fp = log_path.open("a", buffering=1, encoding="utf-8")
    kernel = subprocess.Popen(
        [sys.executable, "-u", "-m", "boot.entry"],
        start_new_session=True,
        stdout=log_fp,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
    )
    try:
        from sbin.tui import main as tui_main
        tui_main()
    finally:
        if kernel.poll() is None:
            # Signal the kernel's whole process group, not just the leader —
            # if the kernel itself is wedged, this still tears down its
            # driver subprocesses (chromium, tmux, etc).
            try:
                pgid = os.getpgid(kernel.pid)
            except ProcessLookupError:
                pgid = None
            if pgid is not None:
                try:
                    os.killpg(pgid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            try:
                kernel.wait(timeout=5)
            except subprocess.TimeoutExpired:
                if pgid is not None:
                    try:
                        os.killpg(pgid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                kernel.wait()
    return kernel.returncode or 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="pai", description="PAI user entrypoint")
    sub = ap.add_subparsers(dest="cmd", required=True)

    start = sub.add_parser("start", help="start kernel and TUI together")
    start.add_argument(
        "--headless",
        action="store_true",
        help="run only the kernel (no TUI); equivalent to `init`",
    )
    start.set_defaults(func=cmd_start)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
