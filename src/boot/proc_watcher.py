"""proc/ watcher — syncs PAI-written specs into the kernel's timer heap.

Watches home/proc/ for new spec.yaml files and status file changes. On a
new spec, parses its deadline or schedule and pushes to the heap. On a
status flip out of the active set (running/scheduled), removes the entry.

This replaces the old event-based `process_spawned` / `process_resolved`
pathway. PAI (or anyone) can just write proc/{slug}/spec.yaml + status
via shell; the kernel picks it up from the filesystem.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Optional

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from . import processes as P
from . import supervisor as S
from . import timers as T


class _Handler(FileSystemEventHandler):
    def __init__(self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue[Path]):
        self.loop = loop
        self.queue = queue

    def _enqueue(self, raw: str) -> None:
        self.loop.call_soon_threadsafe(self.queue.put_nowait, Path(raw))

    def on_created(self, event) -> None:  # type: ignore[override]
        if event.is_directory:
            return
        self._enqueue(event.src_path)

    def on_modified(self, event) -> None:  # type: ignore[override]
        if event.is_directory:
            return
        self._enqueue(event.src_path)

    def on_moved(self, event) -> None:  # type: ignore[override]
        if event.is_directory:
            return
        dest = getattr(event, "dest_path", None)
        if dest:
            self._enqueue(dest)


def _slug_from_path(path: Path) -> Optional[str]:
    """Extract proc slug from a path inside proc/{slug}/..."""
    try:
        rel = path.resolve().relative_to(P.PROC_DIR.resolve())
    except ValueError:
        return None
    parts = rel.parts
    if len(parts) < 2:
        return None
    slug = parts[0]
    if slug.startswith("."):
        return None
    return slug


async def _maybe_supervise(slug: str) -> None:
    """Start or stop supervision based on current spec + status.

    - Running background service (has `run:`, no `schedule:`) → start if not tracked.
    - Any non-running status while tracked → stop (kills subprocess).
    - Missing proc dir while tracked → stop.
    Cron specs with `run:` are launched per-fire by main's timer path, not here.
    """
    try:
        status = P.read_status(slug)
        spec = P.read_spec(slug)
    except P.ProcessNotFound:
        if S.is_tracked(slug):
            await S.stop(slug)
        return

    is_background = "run" in spec and "schedule" not in spec
    if status == "running" and is_background and not S.is_tracked(slug):
        await S.start(slug, spec)
    elif status != "running" and S.is_tracked(slug):
        await S.stop(slug)


def _schedule_spec(heap: list[T.TimerEntry], slug: str) -> None:
    """Push the spec's deadline or next cron fire time onto the heap.

    Idempotent: removes any prior entry for the slug first.
    """
    try:
        status = P.read_status(slug)
        spec = P.read_spec(slug)
    except P.ProcessNotFound:
        return

    T.remove(heap, slug)

    if status not in P.ACTIVE_STATUSES:
        return

    deadline = T._parse_iso(spec.get("deadline"))
    if deadline is not None:
        T.push(heap, deadline, slug)
        try:
            P.append_log(slug, f"kernel: picked up spawn, fires at {deadline.isoformat(timespec='seconds')}")
        except P.ProcessNotFound:
            pass
        return

    schedule = spec.get("schedule")
    if schedule:
        nxt, recurring = T.parse_schedule(schedule, datetime.now())
        if nxt is None:
            try:
                P.append_log(slug, f"kernel: schedule {schedule!r} already past, not arming timer")
            except P.ProcessNotFound:
                pass
            return
        T.push(heap, nxt, slug)
        label = "cron" if recurring else "one-shot schedule"
        try:
            P.append_log(slug, f"kernel: picked up {label}, next fire at {nxt.isoformat(timespec='seconds')}")
        except P.ProcessNotFound:
            pass


async def run(heap: list[T.TimerEntry]) -> None:
    P.PROC_DIR.mkdir(parents=True, exist_ok=True)

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[Path] = asyncio.Queue()
    handler = _Handler(loop, queue)
    observer = Observer()
    observer.schedule(handler, str(P.PROC_DIR), recursive=True)
    observer.start()
    print(f"[proc-watcher] started on {P.PROC_DIR}", flush=True)

    try:
        while True:
            path = await queue.get()
            slug = _slug_from_path(path)
            if slug is None:
                continue
            name = path.name

            if name == "spec.yaml":
                _schedule_spec(heap, slug)
                await _maybe_supervise(slug)
            elif name == "status":
                try:
                    status = P.read_status(slug)
                except P.ProcessNotFound:
                    T.remove(heap, slug)
                    await _maybe_supervise(slug)
                    continue
                if status not in P.ACTIVE_STATUSES:
                    T.remove(heap, slug)
                else:
                    # Transitioned into an active status (running/scheduled) —
                    # reschedule against the heap.
                    _schedule_spec(heap, slug)
                await _maybe_supervise(slug)
    except asyncio.CancelledError:
        raise
    finally:
        observer.stop()
        observer.join(timeout=2)
