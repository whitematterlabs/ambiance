"""Shared bits for the file tools (`read`, `edit`, `write`).

Path resolution: `~` is the PAI's home, absolute paths are real host paths,
and relative paths resolve against the PAI's home (where each fresh `bash`
call also starts). Deprecated FHS-illusion spellings (absolute paths that
only mean something under PAI_ROOT) are rejected with a real-path hint —
never silently translated — mirroring the shell tools.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import paths, stitch
from ._shell_common import classify_fhs_path, log_fhs_reject


@dataclass
class FileToolResult:
    text: str
    is_error: bool = False


class FhsPathError(ValueError):
    """A tool path used a deprecated FHS-illusion spelling; message is the hint."""


def resolve_tool_path(raw: str, env: Optional[dict]) -> Path:
    """Resolve a tool-supplied path to a host path. No jail — parity with
    the shell tools, which are free-solo by design."""
    raw_slug = (env or {}).get("PAI_SLUG")
    try:
        home = stitch.home_for(raw_slug) if raw_slug else paths.HOME_DIR
    except Exception:
        home = paths.HOME_DIR
    raw = raw.strip()
    if raw == "~":
        return Path(home)
    if raw.startswith("~/"):
        return Path(home) / raw[2:]
    if raw.startswith("/"):
        real = classify_fhs_path(raw, str(paths.PAI_ROOT))
        if real is not None:
            log_fhs_reject(raw_slug or "pai", [(raw, real)])
            raise FhsPathError(
                f"{raw} does not exist on this host. FHS-style paths are "
                f"no longer translated — use the real path: {real}"
            )
        return Path(raw)
    return Path(home) / raw


def atomic_write(target: Path, content: str) -> None:
    """Write via tempfile-in-target-dir + fsync + os.replace, preserving the
    target's mode when it already exists."""
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
            encoding="utf-8",
            newline="",  # write bytes as given; no \n translation
        ) as tf:
            tf.write(content)
            tf.flush()
            os.fsync(tf.fileno())
            tmp_path = tf.name
        if target.exists():
            os.chmod(tmp_path, os.stat(target).st_mode & 0o7777)
        os.replace(tmp_path, target)
        tmp_path = None
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
