"""Shared return shape for the two shell tools (`bash`, `shell`)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


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
