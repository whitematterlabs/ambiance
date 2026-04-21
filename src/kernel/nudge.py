"""Nudge — the single entrypoint for waking PAI.

Phase 2: stub that prints and logs. Phase 3 replaces the body with an
LLM call; the kernel loop does not change.
"""

from __future__ import annotations

from typing import Optional

from .processes import ProcessNotFound, append_log


async def nudge(
    reason: str,
    slug: Optional[str] = None,
    context: Optional[dict] = None,
) -> None:
    line = f"[kernel] nudge: {reason}"
    if slug:
        line += f" ({slug})"
    print(line, flush=True)
    if slug:
        try:
            append_log(slug, f"kernel: nudge — {reason}")
        except ProcessNotFound:
            pass
