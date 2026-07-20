"""Shared bits for the file tools (`read`, `edit`, `write`).

Path resolution: `~` is the process HOME, absolute paths are used as-is,
and relative paths resolve against HOME (where each fresh `bash` call
also starts).
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class FileToolResult:
    text: str
    is_error: bool = False


def resolve_tool_path(raw: str, env: Optional[dict]) -> Path:
    """Resolve a tool-supplied path. No jail — parity with the shell
    tools, which are free-solo by design; DAC is the boundary."""
    home = Path((env or {}).get("HOME") or Path.home())
    raw = raw.strip()
    if raw == "~":
        return home
    if raw.startswith("~/"):
        return home / raw[2:]
    if raw.startswith("/"):
        return Path(raw)
    return home / raw


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
