"""doc/ watcher — nudges the librarian whenever a doc drops.

Watches $PAI_ROOT/usr/share/doc/ (recursive) — and $PAI_ROOT/var/lib/doc/,
the durable home for PAI-authored docs that usr/share/doc/built symlinks
into (watchdog/FSEvents does not follow symlinked subdirs, so the durable
dir needs its own watch) — for any file event. On each one, emits a
`doc-watcher:review-doc` event carrying the doc's path relative to
PAI_ROOT. The kernel routes it via wake_on to the librarian, which reads
the doc and decides whether it is skill-worthy.

This is kernel-internal plumbing (like proc_watcher), not an external-surface
driver: it watches an internal dir and only emits events — no heap, no spec,
no supervision. Fire broadly; false wakes are cheaper than missed docs. If the
librarian is woken on its own write, it just NOOPs.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from . import processes as P
from .paths import PAI_ROOT

DOC_DIR: Path = PAI_ROOT / "usr" / "share" / "doc"
# Durable PAI-authored docs (usr/share/doc/built → here). Watched separately:
# the recursive DOC_DIR watch stops at the symlink boundary.
BUILT_DOC_DIR: Path = PAI_ROOT / "var" / "lib" / "doc"


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


def _rel_path(path: Path) -> str:
    """Path relative to PAI_ROOT, for the event payload; absolute if outside."""
    try:
        return str(path.resolve().relative_to(PAI_ROOT.resolve()))
    except ValueError:
        return str(path)


async def run() -> None:
    DOC_DIR.mkdir(parents=True, exist_ok=True)
    BUILT_DOC_DIR.mkdir(parents=True, exist_ok=True)

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[Path] = asyncio.Queue()
    handler = _Handler(loop, queue)
    observer = Observer()
    observer.schedule(handler, str(DOC_DIR), recursive=True)
    observer.schedule(handler, str(BUILT_DOC_DIR), recursive=True)
    observer.start()
    print(f"[doc-watcher] started on {DOC_DIR} + {BUILT_DOC_DIR}", flush=True)

    try:
        while True:
            path = await queue.get()
            P.emit_event(
                {"source": "doc-watcher", "kind": "review-doc", "path": _rel_path(path)}
            )
    except asyncio.CancelledError:
        raise
    finally:
        observer.stop()
        observer.join(timeout=2)
