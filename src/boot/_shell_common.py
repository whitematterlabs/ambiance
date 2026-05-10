"""Shared bits between the two shell tools (`bash`, `shell`).

`ShellResult` is the common return shape; `rewrite_fhs_paths` rewrites
leading-slash FHS paths (`/etc`, `/usr`, ...) to live under PAI's root.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


_FHS_SLOTS = (
    "etc", "usr", "var", "proc", "run", "sys",
    "boot", "sbin", "bin", "opt", "home", "root", "tmp",
)
_FHS_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_./])"
    r"(/(?:" + "|".join(_FHS_SLOTS) + r"))"
    r"(?=/|\s|$|[\"'`\);\],:|&>])"
)


def rewrite_fhs_paths(command: str, root: str) -> str:
    """Rewrite leading-slash FHS paths to live under `root`."""
    return _FHS_PATTERN.sub(lambda m: f"{root}{m.group(1)}", command)


@dataclass
class ShellResult:
    stdout: str
    stderr: str
    exit_code: Optional[int]

    def render(self) -> str:
        out = self.stdout.rstrip("\n")
        err = self.stderr.rstrip("\n")
        parts = []
        if out:
            parts.append(out)
        if err:
            parts.append(f"[stderr]\n{err}")
        if self.exit_code is not None:
            parts.append(f"[exit {self.exit_code}]")
        return "\n".join(parts)
