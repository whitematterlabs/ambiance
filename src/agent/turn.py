"""The turn engine — one member's conversation against the filesystem.

Owns session history (~/.local/state/pai/session/messages.jsonl),
compaction, and provider-failure recovery. Turns are strictly
sequential: one process, one member, one conversation — the v3 per-slug
lock queue died with the fleet.

Compaction is agent-owned, never model-trusted: the model is only ever
asked for a handoff summary; the agent archives and reseeds the history
itself. Three tiers, from `boot/nudge.py`:
  - soft threshold: spend one turn harvesting a summary, then compact
  - hard threshold: compact immediately, breadcrumb seed, no extra turn
  - observed overflow (provider 400): archive, reset empty, retry once
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from . import llm, paths, prompt, tokens
from .image_refs import dehydrate_image_blocks

DEFAULT_COMPACT_THRESHOLD = 150_000
DEFAULT_HARD_COMPACT_THRESHOLD = 400_000

# Cooldown after a compaction: the window gauge only refreshes after the
# next turn reports usage, so without this a just-compacted agent would
# re-fire against the stale pre-compact number.
_COMPACT_COOLDOWN_SECS = 30.0

_COMPACT_INSTRUCTION = (
    "Your conversation history has grown past its compaction threshold. "
    "Reply with a handoff summary of the conversation so far — the agent "
    "will archive the full history after this turn and seed your fresh "
    "context with exactly what you reply here. Keep it focused on what "
    "matters next: open loops, recent decisions, who said what — not "
    "verbatim transcripts. Do not run commands this turn."
)

# Provider "prompt exceeds the context window" markers, matched against the
# stringified exception so no provider exception types leak in here.
_OVERFLOW_MARKERS = (
    "maximum context",
    "context length",
    "context_length_exceeded",
    "prompt is too long",
    "input is too long",
    "too many tokens",
)

# Transient infrastructure failures — retried once, never escalated.
_TRANSIENT_MARKERS = (
    "connection error",
    "timed out",
    "timeout",
    "rate limit",
    "ratelimit",
    "429",
    "overloaded",
    "503",
    "502",
    "temporarily unavailable",
    "expecting value",
    "jsondecodeerror",
)

_TRANSIENT_RETRY_DELAY = 2.0


def _is_overflow(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(m in msg for m in _OVERFLOW_MARKERS)


def _is_transient(exc: BaseException) -> bool:
    if _is_overflow(exc):
        return True
    msg = str(exc).lower()
    return any(m in msg for m in _TRANSIENT_MARKERS)


class Engine:
    def __init__(self, user: str, entry: dict):
        self.user = user
        self.entry = entry
        self.home = paths.home(user)
        self.state_dir = paths.state(user)
        self.history_path = self.state_dir / "session" / "messages.jsonl"
        self._last_compacted = 0.0

    # -- history -----------------------------------------------------------

    def load_history(self) -> list[dict]:
        try:
            text = self.history_path.read_text()
        except OSError:
            return []
        return [json.loads(ln) for ln in text.splitlines() if ln.strip()]

    def save_history(self, messages: list[dict]) -> None:
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        messages = dehydrate_image_blocks(messages)
        data = "".join(json.dumps(m) + "\n" for m in messages)
        fd, tmp = tempfile.mkstemp(
            prefix=".messages.", suffix=".jsonl", dir=str(self.history_path.parent)
        )
        try:
            with os.fdopen(fd, "w") as f:
                f.write(data)
            os.replace(tmp, self.history_path)
        except Exception:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise

    def _archive(self, label: str) -> Optional[str]:
        archive_dir = self.history_path.parent / "history"
        archive_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%dT%H%M%S")
        archive_path = archive_dir / f"{ts}-{label}.jsonl"
        if not self.history_path.exists():
            return None
        shutil.copy(self.history_path, archive_path)
        return str(archive_path)

    def _reseed(self, seed: str) -> None:
        self.save_history([
            {"role": "user", "content": seed},
            {"role": "assistant", "content": "Understood. Continuing."},
        ])
        tokens.reset_window_gauge(self.state_dir)

    # -- compaction --------------------------------------------------------

    async def maybe_compact(self) -> None:
        last_window = tokens.read_last_window(self.state_dir)
        if last_window is None:
            return
        threshold = self.entry.get("compact_threshold") or DEFAULT_COMPACT_THRESHOLD
        hard = (
            self.entry.get("hard_compact_threshold")
            or DEFAULT_HARD_COMPACT_THRESHOLD
        )

        if last_window >= hard:
            self._last_compacted = time.monotonic()
            archived = self._archive("hardcompact")
            self._reseed(
                f"[prior context compacted at {last_window} tokens "
                f"(exceeded {hard}) — no handoff summary was available]"
            )
            print(
                f"agent: hard-compacted ({last_window} >= {hard}) — "
                f"archived to {archived}",
                flush=True,
            )
            return

        cooled = time.monotonic() - self._last_compacted < _COMPACT_COOLDOWN_SECS
        if last_window < threshold or cooled:
            return
        self._last_compacted = time.monotonic()
        print(f"agent: compacting ({last_window} >= {threshold})", flush=True)
        summary = await self.run("compaction", [_COMPACT_INSTRUCTION], compact_turn=True)
        archived = self._archive("compact")
        seed = (
            f"[compacted prior context]\n{summary.strip()}"
            if summary and summary.strip()
            else f"[prior context compacted at {last_window} tokens — "
            f"no handoff summary was available]"
        )
        self._reseed(seed)
        print(f"agent: compacted — archived to {archived}", flush=True)

    # -- the turn ----------------------------------------------------------

    async def run(
        self,
        reason: str,
        bodies: Optional[list[str]] = None,
        drain: Optional[Callable[[], list[str]]] = None,
        compact_turn: bool = False,
    ) -> Optional[str]:
        """Run one turn; returns the reply text (None on cancellation)."""
        system = prompt.build_system_prompt(self.user, self.entry)
        user_turn = prompt.build_user_turn(reason, bodies)
        history = self.load_history()

        reply = ""
        new_history: Optional[list[dict]] = None
        for attempt in range(2):
            try:
                reply, new_history = await llm.run_turn(
                    system,
                    user_turn,
                    history=history,
                    provider=self.entry.get("provider"),
                    model=self.entry.get("model"),
                    state_dir=self.state_dir,
                    home=self.home,
                    drain=None if compact_turn else drain,
                )
                break
            except llm.TurnCancelled as c:
                self.save_history(c.messages)
                print("agent: turn interrupted", flush=True)
                return None
            except Exception as e:
                if attempt == 0 and _is_overflow(e):
                    # The soft compaction never took and the window ran past
                    # the provider's hard limit — recover agent-side.
                    archived = self._archive("overflow")
                    self.save_history([])
                    tokens.reset_window_gauge(self.state_dir)
                    history = []
                    print(
                        f"agent: context overflow — archived to {archived}, "
                        f"reset, retrying",
                        flush=True,
                    )
                    continue
                if attempt == 0 and _is_transient(e):
                    print(f"agent: transient provider error, retrying — {e!r}", flush=True)
                    await asyncio.sleep(_TRANSIENT_RETRY_DELAY)
                    continue
                print(f"agent: turn failed — {e!r}", flush=True)
                return None

        if new_history is None:
            return None
        self.save_history(new_history)
        if reply and not compact_turn:
            print(f"[{self.user}] {reply}", flush=True)
            self._append_transcript(reply)
        return reply

    def _append_transcript(self, text: str) -> None:
        """Member-facing reply log; the console serves this when it lands."""
        path = (
            self.state_dir
            / "transcripts"
            / f"{datetime.now().strftime('%Y-%m-%d')}.md"
        )
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(f"[{datetime.now().strftime('%H:%M')}] pai: {text.strip()}\n")
        except OSError as e:
            print(f"agent: transcript append failed: {e}", flush=True)
