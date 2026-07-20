"""Per-member token accounting, under the member's own state dir.

Two views, both in ~/.local/state/pai/:
    tokens.jsonl   append-only event log, one line per messages.create
    tokens.json    live rollup (last call + running totals)

`last_window_tokens` (input + cache read + cache creation of the most
recent call) is the compaction gauge turn.py reads. Never raises into
the LLM path.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


def _usage_dict(usage: Any) -> dict[str, int]:
    get = (lambda k: getattr(usage, k, 0) or 0) if not isinstance(usage, dict) else (lambda k: usage.get(k, 0) or 0)
    return {
        "input_tokens": int(get("input_tokens")),
        "output_tokens": int(get("output_tokens")),
        "cache_read_input_tokens": int(get("cache_read_input_tokens")),
        "cache_creation_input_tokens": int(get("cache_creation_input_tokens")),
    }


def _rollup_path(state_dir: Path) -> Path:
    return state_dir / "tokens.json"


def read_last_window(state_dir: Path) -> int | None:
    try:
        data = json.loads(_rollup_path(state_dir).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    val = data.get("last_window_tokens")
    try:
        return int(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def reset_window_gauge(state_dir: Path) -> None:
    """Zero the gauge after a history reset/compaction so the next
    threshold check reads post-reset reality, not the stale count."""
    path = _rollup_path(state_dir)
    try:
        data = json.loads(path.read_text())
        data["last_window_tokens"] = 0
        path.write_text(json.dumps(data))
    except Exception:
        pass


def record(state_dir: Path, model: str, usage: Any) -> None:
    try:
        u = _usage_dict(usage)
        ts = time.time()
        state_dir.mkdir(parents=True, exist_ok=True)
        # O_APPEND single-line writes are atomic (well under PIPE_BUF).
        with (state_dir / "tokens.jsonl").open("a") as f:
            f.write(json.dumps({"ts": ts, "model": model, **u}) + "\n")

        path = _rollup_path(state_dir)
        prior: dict[str, Any] = {}
        try:
            prior = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            pass
        rollup = {
            "model": model,
            "last_ts": ts,
            "last_window_tokens": (
                u["input_tokens"]
                + u["cache_read_input_tokens"]
                + u["cache_creation_input_tokens"]
            ),
            "calls": int(prior.get("calls", 0)) + 1,
        }
        for k, v in u.items():
            rollup[f"last_{k}"] = v
            rollup[f"total_{k}"] = int(prior.get(f"total_{k}", 0)) + v
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(rollup, indent=2) + "\n")
        os.replace(tmp, path)
    except Exception as e:
        print(f"agent: tokens.record failed: {e}", flush=True)
