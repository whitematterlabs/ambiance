"""Cursor-based tailer — the shared primitive for outbound drivers.

A Tailer owns a set of files (determined by a predicate), maintains a
byte-offset cursor per file, and calls a user-supplied callback once per
new complete line. Partial trailing lines are left for the next wake.
Cursors are persisted atomically under home/tmp/drivers/{name}/cursors.yaml
so restarts don't replay history or drop in-flight work.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Awaitable, Callable, Optional

import yaml
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

HOME_DIR = Path(__file__).resolve().parent.parent.parent / "home"
DRIVERS_STATE_DIR = HOME_DIR / "tmp" / "drivers"

OnLine = Callable[[Path, str], Awaitable[None]]
OnDirCreated = Callable[[Path], Awaitable[None]]
OwnedPredicate = Callable[[Path], bool]


def _rel(path: Path) -> str:
    return str(path.resolve().relative_to(HOME_DIR.resolve()))


class _Handler(FileSystemEventHandler):
    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        queue: asyncio.Queue[Path],
        dir_queue: asyncio.Queue[Path],
    ):
        self.loop = loop
        self.queue = queue
        self.dir_queue = dir_queue

    def _enqueue(self, raw: str) -> None:
        p = Path(raw)
        self.loop.call_soon_threadsafe(self.queue.put_nowait, p)

    def _enqueue_dir(self, raw: str) -> None:
        p = Path(raw)
        self.loop.call_soon_threadsafe(self.dir_queue.put_nowait, p)

    def on_created(self, event) -> None:  # type: ignore[override]
        if event.is_directory:
            self._enqueue_dir(event.src_path)
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


class Tailer:
    def __init__(
        self,
        name: str,
        roots: list[Path],
        owned: OwnedPredicate,
        on_line: OnLine,
        on_dir_created: Optional[OnDirCreated] = None,
    ):
        self.name = name
        self.roots = [r.resolve() for r in roots]
        self.owned = owned
        self.on_line = on_line
        self.on_dir_created = on_dir_created
        self.state_dir = DRIVERS_STATE_DIR / name
        self.cursors_path = self.state_dir / "cursors.yaml"
        self._cursors: dict[str, int] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._queue: asyncio.Queue[Path] = asyncio.Queue()
        self._dir_queue: asyncio.Queue[Path] = asyncio.Queue()
        self._observer: Optional[Observer] = None
        # One-shot suppressions: `(rel, line)` pairs to skip exactly once.
        # Callbacks register these when they self-append to a file, so the
        # tailer's next read of the appended line is a no-op.
        self._suppress: set[tuple[str, str]] = set()

    # -- cursor persistence -------------------------------------------------

    def _load_cursors(self) -> None:
        if not self.cursors_path.exists():
            self._cursors = {}
            return
        with self.cursors_path.open() as f:
            data = yaml.safe_load(f) or {}
        self._cursors = {str(k): int(v) for k, v in data.items()}

    def _save_cursors(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.cursors_path.with_suffix(".yaml.tmp")
        with tmp.open("w") as f:
            yaml.safe_dump(self._cursors, f, sort_keys=True)
        os.replace(tmp, self.cursors_path)

    # -- suppression --------------------------------------------------------

    def suppress_next(self, path: Path, line: str) -> None:
        """Skip the next occurrence of `line` in `path` without calling on_line.

        Used when on_line self-appends a canonical record of what it just
        processed — the tailer would otherwise re-read it on the next wake.
        """
        self._suppress.add((_rel(path), line))

    # -- file locks ---------------------------------------------------------

    def _lock_for(self, rel: str) -> asyncio.Lock:
        lock = self._locks.get(rel)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[rel] = lock
        return lock

    # -- initial scan -------------------------------------------------------

    def _initial_scan(self) -> None:
        """Seed cursors for all currently owned files at their current size
        (don't replay history). Files that already have cursors keep them.
        """
        for root in self.roots:
            if not root.exists():
                continue
            for path in root.rglob("*"):
                if not path.is_file():
                    continue
                if not self.owned(path):
                    continue
                rel = _rel(path)
                if rel not in self._cursors:
                    self._cursors[rel] = path.stat().st_size
        self._save_cursors()

    # -- drain --------------------------------------------------------------

    async def _drain_file(self, path: Path) -> None:
        if not path.is_file():
            return
        if not self.owned(path):
            return
        rel = _rel(path)
        async with self._lock_for(rel):
            cursor = self._cursors.get(rel)
            size = path.stat().st_size
            if cursor is None:
                # New owned file we haven't seen — replay from 0 is correct
                # because files are born empty.
                cursor = 0
            if size < cursor:
                # File shrunk (rotation, manual edit). Reset to new size
                # rather than replaying — safer than producing duplicates.
                self._cursors[rel] = size
                self._save_cursors()
                return
            if size == cursor:
                return

            with path.open("rb") as f:
                f.seek(cursor)
                chunk = f.read(size - cursor)

            # Only consume up to the last newline; keep trailing partial.
            last_nl = chunk.rfind(b"\n")
            if last_nl < 0:
                return
            consumable = chunk[: last_nl + 1]
            advance = len(consumable)

            try:
                text = consumable.decode("utf-8", errors="replace")
            except Exception:
                self._cursors[rel] = cursor + advance
                self._save_cursors()
                return

            for line in text.splitlines():
                if not line:
                    continue
                key = (rel, line)
                if key in self._suppress:
                    self._suppress.discard(key)
                    continue
                try:
                    await self.on_line(path, line)
                except Exception as e:
                    print(f"[tailer:{self.name}] on_line error for {rel}: {e}", flush=True)
                    # Do not advance cursor on callback failure — retry later.
                    return

            self._cursors[rel] = cursor + advance
            self._save_cursors()

    # -- lifecycle ----------------------------------------------------------

    async def run(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._load_cursors()
        self._initial_scan()

        loop = asyncio.get_running_loop()
        handler = _Handler(loop, self._queue, self._dir_queue)
        observer = Observer()
        for root in self.roots:
            root.mkdir(parents=True, exist_ok=True)
            observer.schedule(handler, str(root), recursive=True)
        observer.start()
        self._observer = observer
        print(f"[tailer:{self.name}] started on {[str(r) for r in self.roots]}", flush=True)

        async def _files() -> None:
            while True:
                path = await self._queue.get()
                await self._drain_file(path)

        async def _dirs() -> None:
            while True:
                d = await self._dir_queue.get()
                if self.on_dir_created is None:
                    continue
                try:
                    await self.on_dir_created(d)
                except Exception as e:
                    print(f"[tailer:{self.name}] on_dir_created error for {d}: {e}", flush=True)

        files_task = asyncio.create_task(_files(), name=f"tailer-{self.name}-files")
        dirs_task = asyncio.create_task(_dirs(), name=f"tailer-{self.name}-dirs")
        try:
            await asyncio.gather(files_task, dirs_task)
        except asyncio.CancelledError:
            raise
        finally:
            for t in (files_task, dirs_task):
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
            if self._observer is not None:
                self._observer.stop()
                self._observer.join(timeout=2)
                self._observer = None
