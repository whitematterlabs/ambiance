"""Filesystem watchers feeding async queues consumed by the TUI widgets.

Each watcher wraps a watchdog Observer running in a background thread.
FS events are marshalled back to the asyncio loop via call_soon_threadsafe
(same pattern as src/kernel/events.py). Widgets await `next()` on a watcher
to receive the next snapshot.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import yaml
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from kernel.processes import EVENTS_DIR, HOME_DIR, PROC_DIR, list_procs, read_spec, read_status

ME_ROOT = HOME_DIR / "communication" / "messages" / "me"
KERNEL_LOG = HOME_DIR / "tmp" / "kernel.log"


def me_thread_dir(pid: int) -> Path:
    return ME_ROOT / str(pid)


def today_file(pid: int) -> Path:
    return me_thread_dir(pid) / f"{date.today().isoformat()}.md"


# --- helpers ---------------------------------------------------------------


class _Poker(FileSystemEventHandler):
    """Generic handler that drops a sentinel onto an asyncio.Queue on any FS event."""

    def __init__(self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue, path_filter=None):
        self.loop = loop
        self.queue = queue
        self.path_filter = path_filter

    def _poke(self, raw_path: str) -> None:
        if self.path_filter and not self.path_filter(Path(raw_path)):
            return
        self.loop.call_soon_threadsafe(self._put_if_room)

    def _put_if_room(self) -> None:
        # Coalesce: if a poke is already pending, skip.
        if self.queue.qsize() == 0:
            self.queue.put_nowait(True)

    def on_any_event(self, event) -> None:  # type: ignore[override]
        # Covers created/modified/moved/deleted for both files and dirs.
        self._poke(getattr(event, "dest_path", None) or event.src_path)


# --- me/ thread ------------------------------------------------------------


@dataclass
class MeSnapshot:
    day_file: Path
    lines: list[str]  # raw lines, no trailing newlines


class MeThreadWatcher:
    """Emits a full MeSnapshot whenever today's me/{pid}/YYYY-MM-DD.md changes."""

    def __init__(self, loop: asyncio.AbstractEventLoop, pid: int):
        self.loop = loop
        self.pid = pid
        self.dir = me_thread_dir(pid)
        self.queue: asyncio.Queue[bool] = asyncio.Queue()
        self._observer: Optional[Observer] = None

    def start(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        my_dir = self.dir
        handler = _Poker(
            self.loop,
            self.queue,
            path_filter=lambda p: p.suffix == ".md" and p.parent == my_dir,
        )
        obs = Observer()
        obs.schedule(handler, str(self.dir), recursive=False)
        obs.start()
        self._observer = obs
        # Prime the queue so on_mount gets an initial snapshot.
        self.queue.put_nowait(True)

    def stop(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=2)
            self._observer = None

    async def next(self) -> MeSnapshot:
        await self.queue.get()
        path = today_file(self.pid)
        if path.exists():
            text = path.read_text(encoding="utf-8", errors="replace")
            lines = [ln for ln in text.splitlines() if ln.strip()]
        else:
            lines = []
        return MeSnapshot(day_file=path, lines=lines)


# --- proc/ -----------------------------------------------------------------


@dataclass
class ProcRow:
    slug: str
    pid: str  # only set for kind:pai procs; "" otherwise
    type: str
    parent: str
    when: str  # deadline ISO or cron schedule, for display
    description: str
    status: str


def _infer_type(spec: dict) -> str:
    if spec.get("kind") == "pai":
        return "pai"
    if spec.get("kind") == "driver":
        return "driver"
    has_run = "run" in spec
    has_schedule = "schedule" in spec
    if has_schedule and has_run:
        return "cron"
    if has_schedule:
        return "timer"
    if has_run:
        return "service"
    if "deadline" in spec:
        return "deadline"
    return "?"


class ProcWatcher:
    """Emits a full list of running processes on any proc/ change."""

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        self.queue: asyncio.Queue[bool] = asyncio.Queue()
        self._observer: Optional[Observer] = None

    def start(self) -> None:
        PROC_DIR.mkdir(parents=True, exist_ok=True)
        handler = _Poker(self.loop, self.queue)
        obs = Observer()
        obs.schedule(handler, str(PROC_DIR), recursive=True)
        obs.start()
        self._observer = obs
        self.queue.put_nowait(True)

    def stop(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=2)
            self._observer = None

    async def next(self) -> list[ProcRow]:
        await self.queue.get()
        rows: list[ProcRow] = []
        for slug in list_procs(status_filter="running"):
            try:
                spec = read_spec(slug)
                status = read_status(slug)
            except Exception:
                continue
            ptype = _infer_type(spec)
            when = str(spec.get("deadline") or spec.get("schedule") or "")
            desc = str(spec.get("description", ""))
            parent = spec.get("parent")
            parent_str = str(parent) if parent is not None else ""
            pid_val = spec.get("pid")
            pid_str = str(pid_val) if isinstance(pid_val, int) else ""
            rows.append(ProcRow(
                slug=slug, pid=pid_str, type=ptype, parent=parent_str,
                when=when, description=desc, status=status,
            ))
        return rows


# --- events/ ---------------------------------------------------------------


@dataclass
class EventSighting:
    at: datetime
    filename: str
    payload: dict


class EventsWatcher:
    """Emits an EventSighting for each new file in home/events/.

    Files may get consumed (deleted) by the kernel before we read them;
    we try to read the YAML in the watchdog thread to beat the kernel,
    and fall back to {"_gone": True} if it's already gone.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        self.queue: asyncio.Queue[EventSighting] = asyncio.Queue()
        self._observer: Optional[Observer] = None

    def start(self) -> None:
        EVENTS_DIR.mkdir(parents=True, exist_ok=True)
        handler = _EventsHandler(self.loop, self.queue)
        obs = Observer()
        obs.schedule(handler, str(EVENTS_DIR), recursive=False)
        obs.start()
        self._observer = obs

    def stop(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=2)
            self._observer = None

    async def next(self) -> EventSighting:
        return await self.queue.get()


def _read_sighting(path: Path) -> EventSighting:
    payload: dict
    try:
        with path.open() as f:
            data = yaml.safe_load(f) or {}
        payload = data if isinstance(data, dict) else {"raw": data}
    except FileNotFoundError:
        payload = {"_gone": True}
    except Exception as e:
        payload = {"_error": repr(e)}
    return EventSighting(at=datetime.now(), filename=path.name, payload=payload)


class _EventsHandler(FileSystemEventHandler):
    def __init__(self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue[EventSighting]):
        self.loop = loop
        self.queue = queue

    def _push(self, raw_path: str) -> None:
        p = Path(raw_path)
        if p.suffix != ".yaml" or p.name.startswith("."):
            return
        # Read in the watchdog thread so we beat the kernel's unlink().
        sighting = _read_sighting(p)
        self.loop.call_soon_threadsafe(self.queue.put_nowait, sighting)

    def on_created(self, event) -> None:  # type: ignore[override]
        if not event.is_directory:
            self._push(event.src_path)

    def on_moved(self, event) -> None:  # type: ignore[override]
        if event.is_directory:
            return
        dest = getattr(event, "dest_path", None)
        if dest:
            self._push(dest)


# --- kernel.log tail -------------------------------------------------------


class LogTailer:
    """In-memory tail of home/tmp/kernel.log. Emits each new line as it's appended."""

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self._observer: Optional[Observer] = None
        self._offset = 0

    def start(self) -> None:
        KERNEL_LOG.parent.mkdir(parents=True, exist_ok=True)
        if not KERNEL_LOG.exists():
            KERNEL_LOG.touch()
        # Start at EOF — only stream lines written after launch.
        self._offset = KERNEL_LOG.stat().st_size

        # No path_filter: macOS FSEvents often reports directory-level
        # events, so filtering on the exact file path drops real writes.
        # Instead, on any event in tmp/ we just re-check kernel.log's size.
        handler = _Poker(self.loop, self.queue)
        handler._put_if_room = self._on_poke  # type: ignore[method-assign]
        obs = Observer()
        obs.schedule(handler, str(KERNEL_LOG.parent), recursive=False)
        obs.start()
        self._observer = obs

        # Also poll as a safety net — FSEvents coalesces bursts and can
        # briefly miss writes. Cheap: one stat + maybe one read per tick.
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def _poll_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(0.5)
                self._read_new()
        except asyncio.CancelledError:
            return

    def stop(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=2)
            self._observer = None
        poll = getattr(self, "_poll_task", None)
        if poll is not None:
            poll.cancel()

    def _on_poke(self) -> None:
        # Runs on the event loop thread.
        self._read_new()

    def _read_new(self) -> None:
        try:
            size = KERNEL_LOG.stat().st_size
        except FileNotFoundError:
            return
        if size < self._offset:
            # Log was truncated / rotated; reset.
            self._offset = 0
        if size == self._offset:
            return
        try:
            with KERNEL_LOG.open("rb") as f:
                f.seek(self._offset)
                chunk = f.read(size - self._offset)
            self._offset = size
        except FileNotFoundError:
            return
        text = chunk.decode("utf-8", errors="replace")
        for line in text.splitlines():
            if line:
                self.queue.put_nowait(line)

    async def next(self) -> str:
        return await self.queue.get()
