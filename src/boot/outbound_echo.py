"""In-memory registry of PAI-drafted iMessage sends, so the inbound
chat.db echo (`is_from_me=1`) for the same line can be dropped instead
of duplicating the canonical record that `imessage/outbound.py` already
wrote with `_append_canonical`.

`tailer.suppress_next` covers the file-tailer echo (re-reading the
canonical line we just wrote). This module covers a different surface —
chat.db reflecting the same send back through the inbound driver a few
seconds later.
"""

from __future__ import annotations

import time

_TTL_SECONDS = 300.0

_pending: dict[tuple[str, str], float] = {}


def _gc(now: float) -> None:
    stale = [k for k, ts in _pending.items() if now - ts > _TTL_SECONDS]
    for k in stale:
        _pending.pop(k, None)


def _norm(text: str) -> str:
    # Outbound registers the day-file form (single line, ` ↵ ` markers);
    # the chat.db echo carries the delivered form (real newlines). Flatten
    # both the same way messages._append_day_file does so they key equal.
    return text.replace("\r\n", "\n").replace("\n", " ↵ ").rstrip()


def register(slug: str, text: str) -> None:
    now = time.monotonic()
    _gc(now)
    _pending[(slug, _norm(text))] = now


def consume(slug: str, text: str) -> bool:
    now = time.monotonic()
    _gc(now)
    return _pending.pop((slug, _norm(text)), None) is not None
