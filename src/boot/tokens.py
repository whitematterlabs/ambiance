"""Per-PAI token accounting.

Every LLM call in the kernel funnels through `llm._loop`, which calls
`record()` once per `messages.create` response. We persist two views:

  /var/log/tokens/<pai>.jsonl   append-only event log, one line per call
  /proc/<pai>/tokens            live rollup (last call + running totals)

Each PAI / subagent has its own conversation and its own context window,
so accounting is strictly per-slug — no roll-up across parents/children.
A single nudge typically triggers multiple `messages.create` calls (the
tool-use loop); each is recorded separately. The rollup's `last_*`
fields reflect the most recent call (which is the relevant signal for
"how full is this conversation's window right now"); the `total_*`
fields sum across the lifetime of the conversation.

This module never raises into the LLM path. If anything goes wrong we
print a one-line warning and return.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from . import paths


def _usage_dict(usage: Any) -> dict[str, int]:
    """Pull the four token counters off an Anthropic usage object,
    defaulting missing fields to 0."""
    get = (lambda k: getattr(usage, k, 0) or 0) if not isinstance(usage, dict) else (lambda k: usage.get(k, 0) or 0)
    return {
        "input_tokens": int(get("input_tokens")),
        "output_tokens": int(get("output_tokens")),
        "cache_read_input_tokens": int(get("cache_read_input_tokens")),
        "cache_creation_input_tokens": int(get("cache_creation_input_tokens")),
    }


def _atomic_write(path: Path, data: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(data)
    os.replace(tmp, path)


def read_last_window(pai_slug: str) -> int | None:
    """Last call's prompt window size from /proc/<pai>/tokens, or None
    if the PAI hasn't made an LLM call yet (file/key absent or unreadable)."""
    # Use processes.PROC_DIR (not paths.proc) so tests monkeypatching the
    # former see consistent state with the rest of the kernel.
    from . import processes as P
    try:
        data = json.loads((P.PROC_DIR / pai_slug / "tokens").read_text())
    except (OSError, json.JSONDecodeError):
        return None
    val = data.get("last_window_tokens")
    try:
        return int(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def record(pai_slug: str | None, model: str, usage: Any) -> None:
    """Persist one LLM call's usage. Safe to call from the hot path —
    swallows all errors. `pai_slug` may be None or "?"; in that case we
    skip recording entirely (we'd rather lose accounting than write to
    a junk slug)."""
    if not pai_slug or pai_slug == "?":
        return
    try:
        u = _usage_dict(usage)
        ts = time.time()
        event = {"ts": ts, "pai": pai_slug, "model": model, **u}

        log_dir = paths.var_log() / "tokens"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{pai_slug}.jsonl"
        # O_APPEND on POSIX makes single-line writes atomic across
        # concurrent writers (one line is well under PIPE_BUF).
        with log_path.open("a") as f:
            f.write(json.dumps(event) + "\n")

        # Rollup lives in /proc/<pai>/, which the kernel manages. If the
        # dir doesn't exist (PAI was deleted, or this is an unmanaged
        # caller), skip the rollup but keep the event log.
        proc_dir = paths.proc(pai_slug)
        if not proc_dir.is_dir():
            return

        rollup_path = proc_dir / "tokens"
        prior: dict[str, Any] = {}
        if rollup_path.exists():
            try:
                prior = json.loads(rollup_path.read_text())
            except (json.JSONDecodeError, OSError):
                prior = {}

        rollup = {
            "pai": pai_slug,
            "model": model,
            "last_ts": ts,
            "last_input_tokens": u["input_tokens"],
            "last_output_tokens": u["output_tokens"],
            "last_cache_read_input_tokens": u["cache_read_input_tokens"],
            "last_cache_creation_input_tokens": u["cache_creation_input_tokens"],
            "last_window_tokens": (
                u["input_tokens"]
                + u["cache_read_input_tokens"]
                + u["cache_creation_input_tokens"]
            ),
            "calls": int(prior.get("calls", 0)) + 1,
            "total_input_tokens": int(prior.get("total_input_tokens", 0)) + u["input_tokens"],
            "total_output_tokens": int(prior.get("total_output_tokens", 0)) + u["output_tokens"],
            "total_cache_read_input_tokens": (
                int(prior.get("total_cache_read_input_tokens", 0)) + u["cache_read_input_tokens"]
            ),
            "total_cache_creation_input_tokens": (
                int(prior.get("total_cache_creation_input_tokens", 0))
                + u["cache_creation_input_tokens"]
            ),
        }
        _atomic_write(rollup_path, json.dumps(rollup, indent=2) + "\n")
    except Exception as e:
        print(f"[kernel] tokens.record failed for {pai_slug}: {e}", flush=True)
