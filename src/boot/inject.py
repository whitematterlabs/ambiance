"""Mid-turn message injection — deliver messages into a running turn.

Turn delivery used to be strictly turn-boundary: a message to a busy PAI
queued behind the per-slug turn lock until the current turn finished. For
long turns that meant minutes of latency, and for ephemeral subagents it
meant never — a subagent ends its life by ending its turn, so the queued
nudge raced the reap and dropped (`no PAI with pid`).

Instead, while a PAI's turn is live (registered by nudge._nudge_body
around the LLM loop) messages addressed to it are queued here. llm._loop
drains the queue at each tool boundary and appends the rendered messages
to the in-flight conversation, so the running turn sees new input within
one model/tool step and keeps going — no cancellation, no lost work.

Everything here runs on the kernel's single event loop with no awaits
between check and mutate, so try_inject vs. turn start/end is race-free
by construction. Entries carry the originating event payload: anything
still queued when the turn ends (arrived after the final drain) is
re-emitted by end_turn's caller and takes the normal nudge path.
"""

from __future__ import annotations

from typing import Optional

from . import bootstrap

# slug -> queue of {"text": rendered user-turn, "event": originating event
# payload or None}. Key presence == a turn is live for that slug.
_live: dict[str, list[dict]] = {}


def register_turn(slug: str) -> bool:
    """Open the injection window for `slug`. Returns False if one is already
    open (nested/concurrent turn — the inner turn shares the outer queue)."""
    if slug in _live:
        return False
    _live[slug] = []
    return True


def end_turn(slug: str) -> list[dict]:
    """Close the injection window. Returns the originating event payloads of
    any entries that were queued but never drained, so the caller can re-emit
    them onto the normal nudge path instead of losing them."""
    entries = _live.pop(slug, [])
    return [e["event"] for e in entries if e.get("event")]


def try_inject(
    target_slug: str,
    reason: str,
    slug: Optional[str] = None,
    context: Optional[dict] = None,
    sender: Optional[str] = None,
    event: Optional[dict] = None,
) -> bool:
    """Queue a message into `target_slug`'s live turn. Returns False when no
    turn is live (caller falls back to a queued nudge) or when the message
    starts an overclock — overclock needs the lock-holding loop in nudge.py,
    which an in-turn text injection can't provide."""
    queue = _live.get(target_slug)
    if queue is None:
        return False
    if isinstance(context, dict) and context.get("overclock") is True:
        return False
    text = bootstrap.build_user_turn(reason, slug, context, sender=sender)
    queue.append({"text": text, "event": event})
    return True


def drain(slug: Optional[str]) -> list[str]:
    """Pop and return every queued rendered message for `slug` (empty when
    nothing is pending or no window is open). Called by llm._loop at tool
    boundaries."""
    if not slug:
        return []
    queue = _live.get(slug)
    if not queue:
        return []
    texts = [e["text"] for e in queue]
    queue.clear()
    return texts
