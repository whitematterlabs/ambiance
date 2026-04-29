"""/sbin/init — entrypoint. Verify layout, exec into the kernel.

After `os.execvp`, this process IS the kernel — there is no separate
init lingering as PID 1. Mirrors Linux: /sbin/init *is* systemd, it
doesn't fork it.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .paths import PAI_ROOT
from .phases.sanity import REQUIRED as REQUIRED_DIRS


def check_layout(root: Path) -> list[str]:
    """Return a list of missing required dirs. Empty list = OK."""
    missing: list[str] = []
    for rel in REQUIRED_DIRS:
        if not (root / rel).exists():
            missing.append(rel)
    return missing


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--check-only",
        action="store_true",
        help="verify layout and exit (do not exec into kernel)",
    )
    args = ap.parse_args()

    missing = check_layout(PAI_ROOT)
    if missing:
        print(
            f"init: PAI_ROOT={PAI_ROOT} missing required dirs: {', '.join(missing)}\n"
            f"      run `paifs-init` to lay out the skeleton.",
            file=sys.stderr,
        )
        return 1

    if args.check_only:
        return 0

    # Hand off: this process becomes the kernel. No return on success.
    os.execvp(sys.executable, [sys.executable, "-u", "-m", "boot.entry"])
    raise AssertionError("execvp returned without replacing process")


if __name__ == "__main__":
    sys.exit(main())
