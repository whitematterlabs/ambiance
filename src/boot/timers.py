"""Min-heap of timer entries sorted by fire time.

Pure, in-memory state owned by the running kernel. Rebuilt from disk
(`home/proc/*/spec.yaml`) at startup so restarts are transparent.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from croniter import croniter

from .processes import ACTIVE_STATUSES, PROC_DIR, read_spec, read_status

# Synthetic heap entries for per-PAI idle heartbeats: slug "heartbeat:<pai>".
# No real proc slug can collide — config rejects ":" in PAI names.
HEARTBEAT_PREFIX = "heartbeat:"


@dataclass(order=True)
class TimerEntry:
    fire_time: datetime
    slug: str = field(compare=False)


def push(heap: list[TimerEntry], fire_time: datetime, slug: str) -> None:
    heapq.heappush(heap, TimerEntry(fire_time, slug))


def pop(heap: list[TimerEntry]) -> TimerEntry:
    return heapq.heappop(heap)


def peek(heap: list[TimerEntry]) -> Optional[TimerEntry]:
    return heap[0] if heap else None


def remove(heap: list[TimerEntry], slug: str) -> int:
    """Remove all entries with the given slug. Returns count removed."""
    before = len(heap)
    heap[:] = [e for e in heap if e.slug != slug]
    heapq.heapify(heap)
    return before - len(heap)


def time_until_next(heap: list[TimerEntry], now: datetime) -> Optional[float]:
    """Seconds to sleep until the next timer. None if heap empty.

    Returns 0 (not negative) if the next timer is already due.
    """
    nxt = peek(heap)
    if nxt is None:
        return None
    delta = (nxt.fire_time - now).total_seconds()
    return max(0.0, delta)


def calc_next_cron(schedule: str, after: datetime) -> datetime:
    return croniter(schedule, after).get_next(datetime)


_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(value) -> int:
    """Parse a duration into seconds: "90s"/"30m"/"1h"/"2d" or a bare int.

    Strings require a unit suffix (a bare "30" string is ambiguous and
    rejected); ints are seconds. Bools and anything non-positive are junk."""
    if isinstance(value, bool):
        raise ValueError(f"invalid duration: {value!r}")
    if isinstance(value, int):
        secs = value
    elif isinstance(value, str):
        v = value.strip().lower()
        if len(v) < 2 or v[-1] not in _DURATION_UNITS:
            raise ValueError(f"invalid duration: {value!r} (use s/m/h/d suffix)")
        try:
            n = int(v[:-1])
        except ValueError:
            raise ValueError(f"invalid duration: {value!r}") from None
        secs = n * _DURATION_UNITS[v[-1]]
    else:
        raise ValueError(f"invalid duration: {value!r}")
    if secs <= 0:
        raise ValueError(f"duration must be positive: {value!r}")
    return secs


def arm_heartbeat(
    heap: list[TimerEntry], slug: str, interval_secs: int,
    now: Optional[datetime] = None,
) -> datetime:
    """(Re)arm the idle heartbeat for a PAI at now + interval.

    Idempotent: removes any prior entry first, so callers never stack beats."""
    if now is None:
        now = datetime.now()
    fire = now + timedelta(seconds=interval_secs)
    remove(heap, HEARTBEAT_PREFIX + slug)
    push(heap, fire, HEARTBEAT_PREFIX + slug)
    return fire


def parse_schedule(schedule: str, now: datetime) -> tuple[Optional[datetime], bool]:
    """Parse a `schedule:` field — either a cron expression or a one-shot ISO datetime.

    Returns (next_fire, is_recurring):
    - cron expression       → (next cron fire, True)
    - ISO datetime in future → (that datetime, False)
    - ISO datetime in past   → (None, False)        # already missed
    """
    try:
        dt = datetime.fromisoformat(schedule)
    except (ValueError, TypeError):
        return (croniter(schedule, now).get_next(datetime), True)
    dt = _to_naive_local(dt)
    return (dt if dt > now else None, False)


def _to_naive_local(dt: datetime) -> datetime:
    """Convert a tz-aware datetime to naive local time. Naive input passes through."""
    if dt.tzinfo is None:
        return dt
    return dt.astimezone().replace(tzinfo=None)


def _parse_iso(value) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _to_naive_local(value)
    return _to_naive_local(datetime.fromisoformat(str(value)))


def rebuild_from_proc() -> list[TimerEntry]:
    """Scan home/proc/ and rebuild the timer heap from active processes.

    Both `running` and `scheduled` procs are eligible — an armed cron rests
    at `scheduled` between fires, so keying off `running` alone would silently
    drop every cron from the heap on the next boot.
    """
    heap: list[TimerEntry] = []
    if not PROC_DIR.exists():
        return heap

    now = datetime.now()
    for child in sorted(PROC_DIR.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        slug = child.name
        try:
            if read_status(slug) not in ACTIVE_STATUSES:
                continue
            spec = read_spec(slug)
        except Exception:
            continue

        # Idle heartbeat — armed now + interval, NOT last-turn-relative:
        # boot-arming from turn history would make every idle PAI due
        # immediately after each deploy re-exec (fleet-wide wake stampede).
        if spec.get("kind") == "pai" and spec.get("heartbeat") is not None:
            try:
                arm_heartbeat(heap, slug, parse_duration(spec["heartbeat"]), now)
            except ValueError:
                pass  # hand-edited junk in a spec must not break boot

        deadline = _parse_iso(spec.get("deadline"))
        if deadline is not None:
            push(heap, deadline, slug)
            continue

        schedule = spec.get("schedule")
        if schedule:
            next_fire, _ = parse_schedule(schedule, now)
            if next_fire is not None:
                push(heap, next_fire, slug)

    return heap
