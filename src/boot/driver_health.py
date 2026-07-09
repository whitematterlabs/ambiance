"""Durable health breadcrumbs for kernel-supervised drivers.

The worst driver failure class is *silent*: a task that crashed once and was
never respawned, a coroutine that returned early and left `/proc` saying
"running", a crash-loop that only shows up as noise in kernel.log. The
supervision paths in `boot.main` call into here at their natural event
boundaries — task start, clean return, cancellation, crash, failed spawn —
and each call appends a cheap, greppable record to
`/proc/<slug>/health.yaml`. No timers, no polling: a breadcrumb is written
exactly when the lifecycle event happens.

Shape (plain YAML — `cat`-able, and what the web console's driver panel
aggregates):

    starts: 4                      # supervise starts over the proc entry's life
    last_start: '2026-07-07T20:38:12'
    recent_starts:                 # bounded ring of start times (crash-loop signal)
      - '2026-07-07T20:36:01'
      - '2026-07-07T20:38:12'
    last_exit: '2026-07-07T20:36:01'
    last_exit_outcome: crashed     # crashed | cancelled | returned | failed_to_start
    last_exit_reason: "RuntimeError('boom')"

`last_exit >= last_start` means the most recent supervise is *gone* — that is
the "silently dead" tell even when the status file still says running (a
clean `return` from a driver coroutine never resolved /proc status).

Every function here is best-effort and never raises: health is a breadcrumb,
not a dependency. A failure to write must not take the driver — or reconcile
— down with it.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import yaml

from . import processes as P

# How many start timestamps the ring keeps. Enough for the web console to see
# "N starts in the last half hour" without health.yaml growing unbounded.
RECENT_STARTS_CAP = 10

# Exit outcomes record_exit accepts. "returned" is a driver coroutine that
# finished on its own — for a long-running ingester that is as wrong as a
# crash, just quieter.
OUTCOMES = ("crashed", "cancelled", "returned", "failed_to_start")


def _now_iso() -> str:
    # Microsecond (not second) resolution is load-bearing. The web console's
    # silent-death heuristic classifies a running driver "down" when
    # last_exit >= last_start. On a graceful kernel restart the OLD kernel
    # records last_exit=cancelled and the NEW kernel records last_start;
    # these are genuinely ordered exit-then-start in wall-clock, but at second
    # resolution they collapse to an equal string whenever they land in the
    # same second — a coin-flip per restart that falsely reds-out the driver.
    # Fixed-width microseconds keep the string compare chronological, so the
    # new start always sorts after the old exit.
    return datetime.now().isoformat(timespec="microseconds")


def health_path(slug: str) -> Path:
    # P.PROC_DIR is read at call time (not import time) so tests that
    # monkeypatch processes.PROC_DIR see the redirect.
    return P.PROC_DIR / slug / "health.yaml"


def read(slug: str) -> dict:
    """The current breadcrumbs for `slug`, or {} if none/unreadable."""
    try:
        with health_path(slug).open() as f:
            data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(slug: str, data: dict) -> None:
    """Atomic-enough write: tmp + rename so a concurrent reader never sees a
    torn file. Silently a no-op when the proc entry doesn't exist yet."""
    path = health_path(slug)
    if not path.parent.is_dir():
        return
    tmp = path.with_name(".health.yaml.tmp")
    try:
        with tmp.open("w") as f:
            yaml.safe_dump(data, f, sort_keys=False)
        tmp.replace(path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def record_start(slug: str, now: str | None = None) -> None:
    """A supervise task for `slug` just started (boot, respawn, paictl start)."""
    try:
        data = read(slug)
        ts = now or _now_iso()
        data["starts"] = int(data.get("starts") or 0) + 1
        data["last_start"] = ts
        recent = data.get("recent_starts")
        recent = list(recent) if isinstance(recent, list) else []
        recent.append(ts)
        data["recent_starts"] = recent[-RECENT_STARTS_CAP:]
        _write(slug, data)
    except Exception:
        pass


def record_exit(slug: str, outcome: str, reason: str = "", now: str | None = None) -> None:
    """The supervise task for `slug` just ended (or failed to spawn)."""
    try:
        data = read(slug)
        data["last_exit"] = now or _now_iso()
        data["last_exit_outcome"] = outcome
        data["last_exit_reason"] = reason
        _write(slug, data)
    except Exception:
        pass
