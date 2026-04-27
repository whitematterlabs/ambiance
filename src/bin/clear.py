#!/usr/bin/env python
"""clear — clear PAI's conversation context at end of current turn.

Queues a clear action. The kernel notices after the turn completes,
archives the current `proc/<pai>/messages.jsonl` under
`proc/<pai>/history/<timestamp>-clear.jsonl`, then empties it so the
next nudge starts fresh.

This does NOT touch anything else — your thread files, journals, logs,
and memory/ all stay put. Only the LLM conversation buffer is reset.

Usage:
    clear
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> int:
    pai = os.environ.get("PAI_SLUG")
    if not pai:
        print("clear: $PAI_SLUG not set — must be invoked from a PAI turn", file=sys.stderr)
        return 1
    proc_dir = Path("proc") / pai
    if not proc_dir.is_dir():
        print(f"clear: no proc dir at {proc_dir}", file=sys.stderr)
        return 1
    (proc_dir / ".history-action").write_text("clear\n")
    print(f"clear: queued — history will be cleared after this turn (pai={pai})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
