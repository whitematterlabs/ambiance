"""Min-heap of timer entries sorted by fire time.

Pure, in-memory state owned by the running kernel. Rebuilt from disk
(`live/proc/*/spec.yaml`) at startup so restarts are transparent.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from croniter import croniter

from .processes import PROC_DIR, read_spec, read_status


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
    return (dt if dt > now else None, False)


def _parse_iso(value) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def rebuild_from_proc() -> list[TimerEntry]:
    """Scan live/proc/ and rebuild the timer heap from running processes."""
    heap: list[TimerEntry] = []
    if not PROC_DIR.exists():
        return heap

    now = datetime.now()
    for child in sorted(PROC_DIR.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        slug = child.name
        try:
            if read_status(slug) != "running":
                continue
            spec = read_spec(slug)
        except Exception:
            continue

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
