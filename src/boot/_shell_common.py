"""Shared bits between the two shell tools (`bash`, `shell`).

`ShellResult` is the common return shape; `classify_fhs_path` /
`find_fhs_spellings` detect deprecated FHS-illusion spellings (`/etc`,
`/usr`, ...) so callers can reject them with a real-path hint. Nothing
here mutates a command or path.
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


def _match_depth(path: str, base: str) -> int:
    """How many leading components of `path` exist under `base` ('' = host)."""
    depth = 0
    cur = base
    for seg in (s for s in path.split("/") if s):
        cur = f"{cur}/{seg}"
        if not os.path.lexists(cur):
            break
        depth += 1
    return depth


def classify_fhs_path(path: str, root: str) -> Optional[str]:
    """The real path under `root` if `path` is an FHS-illusion spelling, else None.

    PAIs historically saw a chroot-like view where `/` mapped to PAI_ROOT;
    the shims that silently translated those spellings are gone (they
    mutated commands in flight and once corrupted a real host path,
    2026-07-08). A spelling is illusory when it resolves deeper under
    PAI_ROOT than on the host. Host full-path existence always wins, and
    ancestor-depth ties go to the host, so real host commands are never
    blocked — only paths that would ENOENT on the host but mean something
    in the PAI's world get refused (with the real path in the hint).
    """
    if not path.startswith("/"):
        return None
    first_seg = path.split("/", 2)[1] if "/" in path[1:] else path[1:]
    if first_seg not in _FHS_SLOTS:
        return None
    if os.path.lexists(path):
        return None
    if _match_depth(path, root) > _match_depth(path, ""):
        return f"{root}{path}"
    return None


def find_fhs_spellings(command: str, root: str) -> list[tuple[str, str]]:
    """Scan a command line for FHS-illusion spellings; never mutates.

    Returns ordered, deduped (token, real_path) pairs using the same
    tokenizer the old rewriter used.
    """
    hits: list[tuple[str, str]] = []
    seen: set[str] = set()
    for m in _FHS_PATTERN.finditer(command):
        token = m.group(1) + (m.group(2) or "")
        if token in seen:
            continue
        real = classify_fhs_path(token, root)
        if real is not None:
            seen.add(token)
            hits.append((token, real))
    return hits


def fhs_reject_message(hits: list[tuple[str, str]]) -> str:
    lines = [
        "path does not exist on this host. FHS-style paths are no longer "
        "translated — use the real path:"
    ]
    lines += [f"  {token} -> {real}" for token, real in hits]
    return "\n".join(lines)


def log_fhs_reject(slug: str, hits: list[tuple[str, str]]) -> None:
    """One kernel.log line + one per-PAI log line per hit.

    Grep surface: `rg fhs-reject` over kernel.log finds fleet-wide
    offenders (per-PAI proc dirs get reaped for subagents; kernel.log
    survives).
    """
    from . import processes as P  # lazy: keep this module import-light

    for token, real in hits:
        print(f"[kernel] fhs-reject slug={slug} {token} -> {real}", flush=True)
        try:
            P.append_log(slug, f"[fhs-reject] {token} -> {real}")
        except Exception:
            pass


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
