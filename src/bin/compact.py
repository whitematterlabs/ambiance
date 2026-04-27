#!/usr/bin/env python
"""compact — compact PAI's conversation context at end of current turn.

Queues a compact action with a summary you provide. The kernel notices
after the turn completes, archives the current `proc/<pai>/messages.jsonl`
under `proc/<pai>/history/<timestamp>-compact.jsonl`, then replaces it
with a short two-message stub built from your summary so the next nudge
starts with the distilled context instead of the full history.

You know what's been going on — distill it yourself and pass it in. Keep
the summary focused on what matters for the next nudge: open loops,
recent decisions, who said what, not verbatim transcripts.

Usage:
    compact "Summary text here..."
    compact <<< "summary from heredoc"
    compact < summary.txt
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> int:
    pai = os.environ.get("PAI_SLUG")
    if not pai:
        print("compact: $PAI_SLUG not set — must be invoked from a PAI turn", file=sys.stderr)
        return 1

    if len(sys.argv) >= 2:
        summary = " ".join(sys.argv[1:])
    else:
        summary = sys.stdin.read()
    summary = summary.strip()
    if not summary:
        print("compact: summary must be non-empty (pass as arg or on stdin)", file=sys.stderr)
        return 1

    proc_dir = Path("proc") / pai
    if not proc_dir.is_dir():
        print(f"compact: no proc dir at {proc_dir}", file=sys.stderr)
        return 1
    (proc_dir / ".history-action").write_text("compact\n" + summary + "\n")
    print(f"compact: queued — history will be compacted after this turn (pai={pai})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
