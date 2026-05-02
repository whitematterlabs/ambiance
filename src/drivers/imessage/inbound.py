"""iMessage inbound driver.

Watches ~/Library/Messages/chat.db{,-wal,-shm} via kqueue. On each
VNODE event, runs a bounded ROWID-delta query against chat.db and
emits one `new_message` event per new inbound row into home/events/.

kqueue (not watchdog/FSEvents) because SQLite modifies the WAL in place
via mmap, which FSEvents coalesces aggressively — you get one event and
then silence. kqueue watches the inode via an open fd and fires on
every NOTE_WRITE/NOTE_EXTEND reliably.

Requires Full Disk Access for the process running the kernel:
System Settings → Privacy & Security → Full Disk Access.

Modern macOS clients write the body as an NSAttributedString typedstream
in `message.attributedBody` and populate the `text` column on a later
WAL write that we'd race. So when `text` is NULL we decode the
typedstream ourselves (see `_decode_attributed_body`).
"""

from __future__ import annotations

import asyncio
import os
import select
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import yaml

from boot import processes as P

CHAT_DB = Path.home() / "Library" / "Messages" / "chat.db"
CURSOR_DIR = P.HOME_DIR / "tmp" / "drivers" / "imessage_in"
CURSOR_PATH = CURSOR_DIR / "cursor.yaml"

MAC_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)

DELTA_SQL = """
SELECT
    m.ROWID AS rowid,
    m.text AS text,
    m.attributedBody AS attributed_body,
    m.is_from_me AS is_from_me,
    m.date AS mac_date,
    h.id AS handle,
    c.guid AS chat_guid,
    (SELECT COUNT(*) FROM chat_handle_join chj WHERE chj.chat_id = c.ROWID) AS participant_count
FROM message m
LEFT JOIN handle h ON m.handle_id = h.ROWID
LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
LEFT JOIN chat c ON c.ROWID = cmj.chat_id
WHERE m.ROWID > ?
ORDER BY m.ROWID ASC
"""


# macOS stores message bodies as a typed-stream NSAttributedString when they
# come from modern clients. The `text` column gets populated on a later WAL
# write — our kqueue drain races that update, so we decode the body ourselves.
# The typedstream header encodes the text as an NSString chunk with pattern:
#   NSString\x01\x94\x84\x01+<len><utf-8 bytes>
# <len> is a single byte, or 0x81/<u16> / 0x82/<u32> for longer strings.
_NS_STRING_MARKER = b"NSString\x01\x94\x84\x01+"


def _decode_attributed_body(data: Optional[bytes]) -> Optional[str]:
    if not data:
        return None
    idx = data.find(_NS_STRING_MARKER)
    if idx < 0:
        return None
    i = idx + len(_NS_STRING_MARKER)
    if i >= len(data):
        return None
    length = data[i]
    i += 1
    if length == 0x81:
        if i + 2 > len(data):
            return None
        length = int.from_bytes(data[i:i + 2], "little")
        i += 2
    elif length == 0x82:
        if i + 4 > len(data):
            return None
        length = int.from_bytes(data[i:i + 4], "little")
        i += 4
    if i + length > len(data):
        return None
    try:
        return data[i:i + length].decode("utf-8")
    except UnicodeDecodeError:
        return None


def _connect() -> sqlite3.Connection:
    # NOTE: opened read-write (not mode=ro) on purpose. In read-only mode
    # SQLite cannot update the wal-index shared-memory file, which means
    # we only see data that Messages.app has already checkpointed out of
    # the WAL — missing live inbound messages until the next checkpoint.
    # query_only=ON makes the connection reject writes.
    conn = sqlite3.connect(str(CHAT_DB))
    conn.execute("PRAGMA query_only = ON")
    conn.row_factory = sqlite3.Row
    return conn


def _load_cursor() -> Optional[int]:
    if not CURSOR_PATH.exists():
        return None
    with CURSOR_PATH.open() as f:
        data = yaml.safe_load(f) or {}
    val = data.get("last_rowid")
    return int(val) if val is not None else None


def _save_cursor(last_rowid: int) -> None:
    # Retry once: on macOS, tmp/ can be reaped (Spotlight, periodic cleanup,
    # external rm -rf) between mkdir and the rename. If the .tmp file vanishes
    # before os.replace, recreate the dir and try again.
    for attempt in range(2):
        CURSOR_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CURSOR_PATH.with_suffix(".yaml.tmp")
        try:
            with tmp.open("w") as f:
                yaml.safe_dump({"last_rowid": last_rowid}, f)
            os.replace(tmp, CURSOR_PATH)
            return
        except FileNotFoundError:
            if attempt == 1:
                raise


def _mac_date_to_iso(mac_date: int) -> str:
    # chat.db stores nanoseconds since 2001-01-01 UTC.
    dt = MAC_EPOCH + timedelta(seconds=mac_date / 1e9)
    return dt.astimezone().isoformat(timespec="seconds")


def _bootstrap_cursor() -> int:
    with _connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(ROWID), 0) AS m FROM message"
        ).fetchone()
    last = int(row["m"])
    _save_cursor(last)
    return last


def _query_rows(last_rowid: int) -> Optional[list]:
    try:
        conn = _connect()
    except sqlite3.OperationalError as e:
        print(f"[imessage-in] cannot open chat.db: {e}", flush=True)
        return None
    try:
        return conn.execute(DELTA_SQL, (last_rowid,)).fetchall()
    except sqlite3.OperationalError as e:
        print(f"[imessage-in] query failed: {e}", flush=True)
        return None
    finally:
        conn.close()


def _row_payload(row) -> Optional[dict]:
    """Build a message dict from a chat.db row, or None if it should be skipped.

    `is_from_me` rows are included — callers decide what to do with them.
    """
    text = row["text"]
    handle = row["handle"] or ""
    if text is None:
        # Modern clients write text in attributedBody; `text` gets filled on
        # a later WAL write that we'd race. Decode the typedstream ourselves.
        text = _decode_attributed_body(row["attributed_body"])
        if text is None:
            return None
    if not handle:
        return None
    payload: dict = {
        "handle": handle,
        "text": text,
        "received_at": _mac_date_to_iso(int(row["mac_date"])),
        "is_from_me": bool(row["is_from_me"]),
    }
    chat_guid = row["chat_guid"] or ""
    if chat_guid and int(row["participant_count"] or 0) > 1:
        payload["chat_guid"] = chat_guid
    return payload


def _drain_live(last_rowid: int) -> int:
    """Per-row event emission for the live stream. Skips is_from_me (outbound
    echo — imessage/outbound already wrote the line)."""
    rows = _query_rows(last_rowid)
    if rows is None:
        return last_rowid
    if rows:
        print(f"[imessage-in] live drain: {len(rows)} rows since rowid={last_rowid}", flush=True)

    new_last = last_rowid
    for row in rows:
        rowid = int(row["rowid"])
        new_last = max(new_last, rowid)
        payload = _row_payload(row)
        if payload is None:
            ab_len = len(row["attributed_body"] or b"")
            print(f"[imessage-in] skipped rowid={rowid} (undecodable body or no handle, ab_len={ab_len})", flush=True)
            continue
        payload = {"source": "imessage", "kind": "new_message", **payload}
        P.emit_event(payload)
        print(f"[imessage-in] emitted rowid={rowid} → {payload['handle']} (from_me={payload.get('is_from_me')})", flush=True)

    if new_last != last_rowid:
        _save_cursor(new_last)
    return new_last


def _drain_catchup(last_rowid: int) -> int:
    """Boot-time pass: coalesce all missed rows into ONE backlog event so
    PAI gets a single nudge instead of N. Includes is_from_me rows (Arda
    texting from his phone while the kernel was down)."""
    rows = _query_rows(last_rowid)
    if rows is None:
        return last_rowid
    if not rows:
        return last_rowid

    print(f"[imessage-in] catchup: {len(rows)} rows since rowid={last_rowid}", flush=True)
    new_last = last_rowid
    messages: list[dict] = []
    for row in rows:
        rowid = int(row["rowid"])
        new_last = max(new_last, rowid)
        payload = _row_payload(row)
        if payload is None:
            continue
        messages.append(payload)

    if messages:
        P.emit_event({
            "source": "imessage",
            "kind": "messages_backlog",
            "messages": messages,
        })
        print(f"[imessage-in] emitted backlog ({len(messages)} messages)", flush=True)

    if new_last != last_rowid:
        _save_cursor(new_last)
    return new_last


# Watch chat.db-wal only. chat.db-shm gets touched by every SQLite read
# (including ours), which creates a feedback loop via NOTE_ATTRIB. chat.db
# itself only changes during checkpoint, at which point the WAL file also
# moves (NOTE_RENAME / NOTE_DELETE) and our reopen path handles it.
WATCHED_NAMES = ("chat.db-wal",)

# NOTE_WRITE covers in-place WAL appends (new messages).
# NOTE_DELETE / NOTE_RENAME fire when Messages.app rotates the WAL during
# a checkpoint — we re-open the fd when that happens.
# NOTE_ATTRIB is excluded on purpose: SQLite opens of chat.db fstat the
# WAL, producing ATTRIB events we'd otherwise chase infinitely.
VNODE_FLAGS = (
    select.KQ_NOTE_WRITE
    | select.KQ_NOTE_EXTEND
    | select.KQ_NOTE_DELETE
    | select.KQ_NOTE_RENAME
)


class _KqueueWatcher:
    """Watches the chat.db family via kqueue, posts a sentinel to the
    asyncio queue on every event. Runs a background OS thread that blocks
    on kqueue.control()."""

    def __init__(self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue):
        self.loop = loop
        self.queue = queue
        self._kq: Optional[select.kqueue] = None
        self._fds: dict[int, str] = {}  # fd -> filename
        self._thread: Optional[threading.Thread] = None
        self._stop_fd_r: Optional[int] = None
        self._stop_fd_w: Optional[int] = None

    def _open_target(self, name: str) -> Optional[int]:
        path = CHAT_DB.parent / name
        try:
            fd = os.open(str(path), os.O_RDONLY)
        except FileNotFoundError:
            return None
        return fd

    def _register(self, fd: int, name: str) -> None:
        assert self._kq is not None
        kev = select.kevent(
            fd,
            filter=select.KQ_FILTER_VNODE,
            flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_CLEAR,
            fflags=VNODE_FLAGS,
        )
        self._kq.control([kev], 0)
        self._fds[fd] = name

    def start(self) -> None:
        self._kq = select.kqueue()
        # Pipe used to unblock the kqueue.control() on stop().
        r, w = os.pipe()
        self._stop_fd_r, self._stop_fd_w = r, w
        stop_kev = select.kevent(
            r,
            filter=select.KQ_FILTER_READ,
            flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE,
        )
        self._kq.control([stop_kev], 0)

        for name in WATCHED_NAMES:
            fd = self._open_target(name)
            if fd is not None:
                self._register(fd, name)
                print(f"[imessage-in] kqueue watching {name} fd={fd}", flush=True)
            else:
                print(f"[imessage-in] kqueue target {name} missing — skipping", flush=True)

        self._thread = threading.Thread(target=self._loop, name="imessage-in-kq", daemon=True)
        self._thread.start()

    def _reopen(self, old_fd: int, name: str) -> None:
        try:
            os.close(old_fd)
        except OSError:
            pass
        self._fds.pop(old_fd, None)
        fd = self._open_target(name)
        if fd is not None:
            self._register(fd, name)
            print(f"[imessage-in] kqueue re-opened {name} fd={fd}", flush=True)

    def _loop(self) -> None:
        assert self._kq is not None
        assert self._stop_fd_r is not None
        try:
            while True:
                events = self._kq.control([], 16, None)  # block forever
                stop = False
                for ev in events:
                    if ev.ident == self._stop_fd_r:
                        stop = True
                        continue
                    fd = ev.ident
                    name = self._fds.get(fd, f"fd={fd}")
                    fflags = ev.fflags
                    # Wake the drain.
                    self.loop.call_soon_threadsafe(self.queue.put_nowait, None)
                    # If the file was rotated, reopen so we keep receiving events.
                    if fflags & (select.KQ_NOTE_DELETE | select.KQ_NOTE_RENAME):
                        print(f"[imessage-in] kq {name} rotated, reopening", flush=True)
                        self._reopen(fd, name)
                if stop:
                    return
        except Exception as e:
            print(f"[imessage-in] kq-loop crashed: {e!r}", flush=True)

    def stop(self) -> None:
        if self._stop_fd_w is not None:
            try:
                os.write(self._stop_fd_w, b"x")
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2)
        for fd in list(self._fds):
            try:
                os.close(fd)
            except OSError:
                pass
        self._fds.clear()
        for fd in (self._stop_fd_r, self._stop_fd_w):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self._stop_fd_r = self._stop_fd_w = None
        if self._kq is not None:
            self._kq.close()
            self._kq = None


async def run() -> None:
    if not CHAT_DB.exists():
        print(f"[imessage-in] chat.db not found at {CHAT_DB}; driver idle", flush=True)
        return

    last_rowid = _load_cursor()
    if last_rowid is None:
        try:
            last_rowid = _bootstrap_cursor()
            print(f"[imessage-in] bootstrap cursor last_rowid={last_rowid}", flush=True)
        except sqlite3.OperationalError as e:
            print(f"[imessage-in] cannot bootstrap (FDA granted?): {e}", flush=True)
            return

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    watcher = _KqueueWatcher(loop, queue)
    watcher.start()
    print(f"[imessage-in] started, last_rowid={last_rowid}", flush=True)

    # Catch-up pass in case messages arrived while the kernel was down.
    # Emits a single backlog event so PAI gets one nudge, not N.
    last_rowid = await asyncio.to_thread(_drain_catchup, last_rowid)

    try:
        while True:
            await queue.get()
            # Coalesce bursts of WAL writes into a single SQL pass.
            while not queue.empty():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            last_rowid = await asyncio.to_thread(_drain_live, last_rowid)
    except asyncio.CancelledError:
        raise
    finally:
        watcher.stop()
        print("[imessage-in] stopped", flush=True)
