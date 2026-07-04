#!/usr/bin/env python
"""clear — clear PAI's conversation context at end of current turn.

Queues a clear action. The kernel notices after the turn completes,
archives the current `proc/<pai>/messages.jsonl` under
`proc/<pai>/history/<timestamp>-clear.jsonl`, then empties it so the
next nudge starts fresh.

This does NOT touch anything else — your thread files, journals, logs,
and memory/ all stay put. Only the LLM conversation buffer is reset.

Usage:
    clear          — clear this PAI's history
    clear all      — clear history for every running PAI
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import yaml

from boot import paths

_TERMINAL = {"completed", "expired", "cancelled", "failed", "stopped"}


def _queue_clear(proc_dir: Path) -> None:
    (proc_dir / ".history-action").write_text("clear\n")


def _is_live(proc_dir: Path) -> bool:
    status = (proc_dir / "status").read_text().strip() if (proc_dir / "status").exists() else "running"
    return status not in _TERMINAL


def main() -> int:
    pai_root = paths.PAI_ROOT
    proc_root = pai_root / "proc"

    if len(sys.argv) > 1 and sys.argv[1] == "all":
        cleared = []
        for spec_path in sorted(proc_root.glob("*/spec.yaml")):
            try:
                spec = yaml.safe_load(spec_path.read_text()) or {}
            except Exception:
                continue
            if spec.get("kind") != "pai":
                continue
            if not _is_live(spec_path.parent):
                continue
            _queue_clear(spec_path.parent)
            cleared.append(spec_path.parent.name)
        if not cleared:
            print("clear all: no running PAIs found", file=sys.stderr)
            return 1
        for slug in cleared:
            print(f"clear: queued — history will be cleared after this turn (pai={slug})")
        return 0

    pai = os.environ.get("PAI_SLUG")
    if not pai:
        print("clear: $PAI_SLUG not set — must be invoked from a PAI turn", file=sys.stderr)
        return 1
    proc_dir = proc_root / pai
    if not proc_dir.is_dir():
        print(f"clear: no proc dir at {proc_dir}", file=sys.stderr)
        return 1
    _queue_clear(proc_dir)
    print(f"clear: queued — history will be cleared after this turn (pai={pai})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
