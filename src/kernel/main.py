"""The kernel loop — tickless, event + timer driven.

Sleeps on whichever fires first: an FS event in live/events/ or the next
pending timer. When the heap is empty and no events are pending, blocks
indefinitely on the watcher.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

from . import processes as P
from . import timers as T
from .events import EventWatcher, read_event
from .nudge import nudge


async def _handle_timer(entry: T.TimerEntry, heap: list[T.TimerEntry]) -> None:
    slug = entry.slug
    try:
        status = P.read_status(slug)
        spec = P.read_spec(slug)
    except P.ProcessNotFound:
        return

    if status != "running":
        return  # stale timer; process was resolved

    if spec.get("type") == "cron":
        await nudge("cron fired", slug, spec)
        schedule = spec.get("schedule")
        if schedule:
            next_fire = T.calc_next_cron(schedule, datetime.now())
            T.push(heap, next_fire, slug)
            P.append_log(slug, f"kernel: next fire at {next_fire.isoformat(timespec='seconds')}")
    else:
        await nudge("deadline reached", slug, spec)
        # Phase 2: naive resolution. Phase 3 will consult resolve_on.
        try:
            P.resolve(slug, "expired")
        except P.ProcessNotFound:
            pass


async def _drain_elapsed_timers(heap: list[T.TimerEntry], now: datetime) -> None:
    while True:
        nxt = T.peek(heap)
        if nxt is None or nxt.fire_time > now:
            return
        entry = T.pop(heap)
        await _handle_timer(entry, heap)


async def _handle_event_file(path: Path, heap: list[T.TimerEntry]) -> None:
    event = read_event(path)
    kind = event.get("kind")

    if kind == "process_spawned":
        slug = event.get("slug")
        if not slug:
            return
        try:
            spec = P.read_spec(slug)
        except P.ProcessNotFound:
            return
        deadline = T._parse_iso(spec.get("deadline"))
        if deadline is not None:
            T.push(heap, deadline, slug)
            return
        schedule = spec.get("schedule")
        if schedule:
            T.push(heap, T.calc_next_cron(schedule, datetime.now()), slug)

    elif kind == "process_resolved":
        slug = event.get("slug")
        if slug:
            T.remove(heap, slug)

    else:
        await nudge(f"event: {kind or 'unknown'}", context=event)


async def run() -> None:
    loop = asyncio.get_running_loop()
    heap = T.rebuild_from_proc()
    watcher = EventWatcher(P.EVENTS_DIR, loop)
    watcher.start()
    print(f"[kernel] started — {len(heap)} timers loaded", flush=True)

    try:
        while True:
            now = datetime.now()
            await _drain_elapsed_timers(heap, now)
            timeout = T.time_until_next(heap, datetime.now())

            event_task = asyncio.create_task(watcher.next())
            if timeout is None:
                await event_task
                await _handle_event_file(event_task.result(), heap)
            else:
                sleep_task = asyncio.create_task(asyncio.sleep(timeout))
                done, pending = await asyncio.wait(
                    {event_task, sleep_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in pending:
                    t.cancel()
                if event_task in done:
                    await _handle_event_file(event_task.result(), heap)
    except asyncio.CancelledError:
        raise
    finally:
        watcher.stop()
        print("[kernel] stopped", flush=True)
