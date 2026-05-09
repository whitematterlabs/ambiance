"""Async FS watcher over home/events/.

Events are plain YAML files dropped into the directory. The watchdog
observer runs in a background thread and pushes new paths onto an
asyncio.Queue that the kernel loop awaits.
"""

from __future__ import annotations

import asyncio
import collections
import time
from pathlib import Path
from typing import Optional

import yaml
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer


def _is_event_file(path: Path) -> bool:
    return (
        path.is_file()
        and path.suffix == ".yaml"
        and not path.name.startswith(".")
    )


class _Handler(FileSystemEventHandler):
    def __init__(self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue[Path]):
        self.loop = loop
        self.queue = queue
        # Defense-in-depth against FSEvents redelivering the same path:
        # filenames embed microseconds so distinct events never collide.
        self._seen: "collections.OrderedDict[str, float]" = collections.OrderedDict()

    def _enqueue(self, raw_path: str) -> None:
        path = Path(raw_path)
        if not _is_event_file(path):
            return
        now = time.monotonic()
        while self._seen and next(iter(self._seen.values())) < now - 5.0:
            self._seen.popitem(last=False)
        if raw_path in self._seen:
            return
        self._seen[raw_path] = now
        self.loop.call_soon_threadsafe(self.queue.put_nowait, path)

    def on_created(self, event) -> None:  # type: ignore[override]
        if event.is_directory:
            return
        self._enqueue(event.src_path)

    def on_moved(self, event) -> None:  # type: ignore[override]
        if event.is_directory:
            return
        # e.g. atomic writes land as a rename — the dest is the real file
        dest = getattr(event, "dest_path", None)
        if dest:
            self._enqueue(dest)


class EventWatcher:
    def __init__(self, events_dir: Path, loop: asyncio.AbstractEventLoop):
        self.events_dir = events_dir
        self.loop = loop
        self.queue: asyncio.Queue[Path] = asyncio.Queue()
        self._observer: Optional[Observer] = None

    def start(self) -> None:
        self.events_dir.mkdir(parents=True, exist_ok=True)
        # Catch-up: enqueue anything already on disk
        for path in sorted(self.events_dir.iterdir()):
            if _is_event_file(path):
                self.queue.put_nowait(path)

        handler = _Handler(self.loop, self.queue)
        obs = Observer()
        obs.schedule(handler, str(self.events_dir), recursive=False)
        obs.start()
        self._observer = obs

    def stop(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=2)
            self._observer = None

    async def next(self) -> Path:
        return await self.queue.get()


def read_event(path: Path) -> Optional[dict]:
    """Parse an event file and consume it (delete from disk).

    Returns None if the file is gone — racy proc-watcher / consumer
    interleavings can hand us a path that's already been read+unlinked.
    """
    try:
        with path.open() as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        return None
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    if not isinstance(data, dict):
        return {"raw": data}
    return data
