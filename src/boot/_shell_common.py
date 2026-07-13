"""Shared bits between the two shell tools (`bash`, `shell`).

`ShellResult` is the common return shape; `rewrite_fhs_paths` rewrites
leading-slash FHS paths (`/etc`, `/usr`, ...) to live under PAI's root.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Optional


_FHS_SLOTS = (
    "etc", "usr", "var", "proc", "run", "sys",
    "boot", "sbin", "bin", "opt", "home", "root", "tmp",
)
# group 1: the /slot itself. group 2: the rest of the path (everything up to a
# shell delimiter), so the whole path token is group1+group2 — needed to test
# whether the PAI-view or the real host path exists. The lookbehind keeps us
# off relative paths (`foo/usr`) and the trailing lookahead keeps `/opt` from
# matching inside `/optional`.
_PATH_TAIL = r"[^\s\"'`;|&><)\]},:]*"
_FHS_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_./])"
    r"(/(?:" + "|".join(_FHS_SLOTS) + r"))"
    r"(/" + _PATH_TAIL + r")?"
    r"(?=[\s\"'`;|&><)\]},:]|$)"
)


def rewrite_fhs_paths(command: str, root: str) -> str:
    """Rewrite leading-slash FHS paths to live under PAI's root `root`.

    A PAI sees a chroot-like FHS view where `/` maps to PAI_ROOT, so `/usr/...`
    in a command means `<root>/usr/...`. But the same syntax also names real
    host paths a PAI legitimately reads off the system and echoes back —
    `/opt/homebrew/bin/node`, `/tmp/...`, a `/var/folders/...` macOS tempdir.
    Blindly prefixing PAI_ROOT corrupts those (2026-07-08: it turned a correct
    `/opt/homebrew/bin/node` into `<root>/opt/homebrew/bin/node`, a nonexistent
    path that crash-looped a supervised service ~220×/s for 45 minutes).

    So the rewrite is existence-guarded: rewrite to the PAI-view path when that
    path actually exists; leave a real host path alone when *it* exists and the
    PAI-view one does not; and for a path that exists neither way (a file about
    to be created), default to the PAI-view path so `/tmp/out`, `/home/<pai>/x`
    etc. keep their chroot semantics.
    """
    def _sub(m: re.Match) -> str:
        full = m.group(1) + (m.group(2) or "")
        pai_path = f"{root}{full}"
        if os.path.lexists(pai_path):
            return pai_path
        if os.path.lexists(full):
            return full
        return pai_path

    return _FHS_PATTERN.sub(_sub, command)


def rewrite_fhs_path(path: str, root: str) -> str:
    """Single-path variant of `rewrite_fhs_paths` for the file tools.

    The input is one whole path, not a command line, so no regex tokenizing —
    `_PATH_TAIL` would mis-split a bare path containing `:` or `,`. Same
    existence-guarded tri-state: PAI-view exists → PAI-view; else host exists
    → host; else default to the PAI-view (a file about to be created).
    Non-FHS absolute paths and relative paths are returned unchanged.
    """
    if not path.startswith("/"):
        return path
    first_seg = path.split("/", 2)[1]
    if first_seg not in _FHS_SLOTS:
        return path
    pai_path = f"{root}{path}"
    if os.path.lexists(pai_path):
        return pai_path
    if os.path.lexists(path):
        return path
    return pai_path


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
