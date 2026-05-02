"""macOS Mail.app inbound driver.

Watches ~/Library/Mail/V10/MailData/Envelope Index{,-wal,-shm} via kqueue.
On each VNODE event, runs a bounded ROWID-delta query against the index
and emits one `new_email` event per new row whose mailbox is INBOX or a
Sent folder. Sent rows produce outbound yamls — Mail.app's Sent folder is
the canonical record of "what got sent", regardless of whether PAI's
draft pipeline or Arda originated it.

kqueue (not watchdog/FSEvents) for the same reason as imessage: SQLite
modifies the WAL in place via mmap, FSEvents coalesces aggressively.

Requires Full Disk Access. Same Privacy & Security toggle as imessage.
"""

from __future__ import annotations

import asyncio
import os
import select
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse

import yaml

from boot import paths
from boot import processes as P

from .. import shared
from . import accounts as A
from . import emlx as E

ENVELOPE_DIR = Path.home() / "Library" / "Mail" / "V10" / "MailData"
ENVELOPE_INDEX = ENVELOPE_DIR / "Envelope Index"

CURSOR_DIR = P.HOME_DIR / "tmp" / "drivers" / "macmail"
CURSOR_PATH = CURSOR_DIR / "cursor.yaml"


def _build_delta_sql(cfg: A.AccountsConfig) -> tuple[str, list[str]]:
    """Build the cursor-bounded delta query for the current account list.

    Each known (account, role) pair contributes one `mb.url LIKE ?`
    clause. Roles are derived from Mail.app's own `inbox` / `sent mailbox`
    references — locale-independent.
    """
    patterns = [pat for pat, _role in cfg.url_like_patterns()]
    if not patterns:
        # No accounts known yet (Mail.app not enumerated). Match nothing —
        # `1=0` keeps the query well-formed but returns zero rows.
        clause = "1 = 0"
    else:
        clause = "(" + " OR ".join(["mb.url LIKE ?"] * len(patterns)) + ")"
    sql = f"""
SELECT
    m.ROWID AS rowid,
    m.date_received AS date_received,
    m.conversation_id AS conversation_id,
    mb.url AS url
FROM messages m
JOIN mailboxes mb ON mb.ROWID = m.mailbox
WHERE m.ROWID > ?
  AND {clause}
ORDER BY m.ROWID ASC
"""
    return sql, patterns


def _connect() -> sqlite3.Connection:
    # Same rationale as imessage: open read-write so SQLite can update the
    # wal-index, then use PRAGMA query_only to forbid writes. Read-only
    # mode would only see checkpointed data — we'd miss live mail.
    conn = sqlite3.connect(str(ENVELOPE_INDEX))
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
    CURSOR_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CURSOR_PATH.with_suffix(".yaml.tmp")
    with tmp.open("w") as f:
        yaml.safe_dump({"last_rowid": last_rowid}, f)
    os.replace(tmp, CURSOR_PATH)


def _bootstrap_cursor() -> int:
    with _connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(ROWID), 0) AS m FROM messages"
        ).fetchone()
    last = int(row["m"])
    _save_cursor(last)
    return last


def _mac_date_to_dt(secs: int) -> datetime:
    # Despite the column name lineage, Mail.app V10 stores date_received as
    # Unix epoch seconds, not Mac epoch (2001-01-01). Treating it as Mac
    # epoch shifts every timestamp forward by 31 years.
    return datetime.fromtimestamp(int(secs), tz=timezone.utc).astimezone()


# ---------- mailbox URL → (account_uuid, direction) -----------------------

def _parse_url(url: str, cfg: A.AccountsConfig) -> tuple[str, Optional[str]]:
    """Return (account_uuid, direction) for a `mailboxes.url` value.

    Account UUID is the netloc (minus any user@ prefix). Direction is
    derived from `accounts.role_for_url`, which checks the URL against
    Mail.app's own concept of inbox/sent — works regardless of locale.
    Returns (uuid, None) if the URL doesn't match any known role
    (caller skips the row).
    """
    parsed = urlparse(url)
    netloc = parsed.netloc
    if "@" in netloc:
        netloc = netloc.split("@", 1)[0]
    return netloc, cfg.role_for_url(url)


# ---------- per-message build + write --------------------------------------

def _ensure_account_meta(account_dir: Path, account_address: str, account_uuid: str) -> None:
    meta_path = account_dir / "meta.yaml"
    if meta_path.exists():
        return
    account_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "account": account_address,
        "provider": "macmail",
        "account_uuid": account_uuid,
        "created": datetime.now().date().isoformat(),
    }
    tmp = meta_path.with_suffix(".yaml.tmp")
    with tmp.open("w") as f:
        yaml.safe_dump(meta, f, sort_keys=False)
    os.replace(tmp, meta_path)
    print(f"[macmail-in] auto-created meta.yaml for {account_address}", flush=True)


def _build_msg_dict(msg, direction: str, ts: datetime, conversation_id: int) -> dict:
    message_id = (msg.get("Message-ID") or msg.get("Message-Id") or "").strip()
    in_reply_to = (msg.get("In-Reply-To") or "").strip() or None
    references = E.header_id_list(msg.get("References"))
    from_addrs = E.extract_addresses(msg.get("From"))
    to_addrs = E.extract_addresses(msg.get("To"))
    cc_addrs = E.extract_addresses(msg.get("Cc"))
    bcc_addrs = E.extract_addresses(msg.get("Bcc"))
    subject = str(msg.get("Subject") or "")
    content = E.extract_text(msg)

    out: dict = {
        "message_id": message_id,
        "in_reply_to": in_reply_to,
        "references": references,
        "thread_slug": shared.thread_slug(subject, references, message_id),
        "from": from_addrs[0]["address"] if from_addrs else "",
        "from_name": from_addrs[0].get("name") if from_addrs else None,
        "to": [a["address"] for a in to_addrs],
        "cc": [a["address"] for a in cc_addrs],
        "bcc": [a["address"] for a in bcc_addrs],
        "subject": subject,
        "direction": direction,
        "content": content,
        "attachments": [],
        "provider_thread_id": str(conversation_id) if conversation_id else None,
    }
    if direction == "outbound":
        out["sent_at"] = ts.isoformat(timespec="seconds")
    else:
        out["received_at"] = ts.isoformat(timespec="seconds")
    return out


def ingest_row(row, cfg: A.AccountsConfig) -> Optional[dict]:
    """Process one delta row. Returns an event-payload dict on success or
    None if we should leave the cursor parked (e.g. partial emlx).

    Public so the `mailsearch` tool can reuse the row-to-yaml pipeline.
    """
    rowid = int(row["rowid"])
    url = row["url"] or ""
    account_uuid, direction = _parse_url(url, cfg)
    if direction is None:
        # URL didn't match any known inbox/sent role — shouldn't happen
        # for rows the SQL filter accepts, but skip defensively.
        print(f"[macmail-in] no role for url={url!r}; skipping rowid={rowid}", flush=True)
        return {"_skip": True}

    # Mailbox-name path component, used by emlx layout. Mail.app stores
    # `.mbox` directories on disk by the same name shown in the URL.
    mailbox_name = unquote(urlparse(url).path.lstrip("/"))

    path = E.emlx_path(account_uuid, mailbox_name, rowid)
    if path is None:
        # Either the body is still .partial.emlx or Mail hasn't flushed
        # the file yet. Park; next WAL kick will retry.
        return None

    try:
        data = path.read_bytes()
    except OSError as e:
        print(f"[macmail-in] cannot read {path}: {e}", flush=True)
        return None
    try:
        msg = E.parse_emlx(data)
    except ValueError as e:
        print(f"[macmail-in] parse failed rowid={rowid}: {e}", flush=True)
        # Bad framing — advance past it; we won't recover by retrying.
        return {"_skip": True}

    address = cfg.address_for_uuid(account_uuid)
    if address is None:
        # AppleScript discovery hasn't enumerated this account yet (rare;
        # account added since last refresh). Skip; the next refresh will
        # pick it up and a future kqueue tick will retry via boot scan.
        print(f"[macmail-in] no canonical address for uuid={account_uuid}; skipping rowid={rowid}", flush=True)
        return {"_skip": True}

    ts = _mac_date_to_dt(int(row["date_received"] or 0))
    msg_dict = _build_msg_dict(msg, direction, ts, int(row["conversation_id"] or 0))

    account_dir = paths.var_spool_email() / address
    _ensure_account_meta(account_dir, address, account_uuid)

    msg_path, created = shared.write_message_yaml(account_dir, msg_dict)
    shared.link_thread(account_dir, msg_path, msg_dict["thread_slug"], ts)

    parent_path: Optional[Path] = None
    parent_id = msg_dict["in_reply_to"] or (msg_dict["references"][-1] if msg_dict["references"] else None)
    if parent_id:
        parent_path = shared.find_message_by_id(account_dir, parent_id)
        if parent_path:
            shared.link_prev(msg_path, parent_path)

    return {
        "account": address,
        "thread_slug": msg_dict["thread_slug"],
        "subject": msg_dict["subject"],
        "from": msg_dict["from"],
        "direction": direction,
        "path": str(msg_path.relative_to(paths.PAI_ROOT)),
        "_created": created,
    }


# ---------- live + catchup drains ------------------------------------------

def _query_rows(last_rowid: int, cfg: A.AccountsConfig) -> Optional[list]:
    try:
        conn = _connect()
    except sqlite3.OperationalError as e:
        print(f"[macmail-in] cannot open Envelope Index: {e}", flush=True)
        return None
    sql, patterns = _build_delta_sql(cfg)
    try:
        return conn.execute(sql, (last_rowid, *patterns)).fetchall()
    except sqlite3.OperationalError as e:
        print(f"[macmail-in] query failed: {e}", flush=True)
        return None
    finally:
        conn.close()


_last_live_log: tuple[int, int] | None = None


def _drain_live(last_rowid: int, cfg: A.AccountsConfig) -> int:
    global _last_live_log
    rows = _query_rows(last_rowid, cfg)
    if rows is None:
        return last_rowid
    if rows:
        sig = (last_rowid, len(rows))
        if sig != _last_live_log:
            print(f"[macmail-in] live drain: {len(rows)} rows since rowid={last_rowid}", flush=True)
            _last_live_log = sig

    lowest_parked: Optional[int] = None
    max_processed = last_rowid
    for row in rows:
        rowid = int(row["rowid"])
        result = ingest_row(row, cfg)
        if result is None:
            # Body not on disk yet (.partial.emlx). Don't advance cursor past
            # this rowid — but keep scanning later rows; subsequent emlx
            # files may already be ready and writes are idempotent.
            if lowest_parked is None or rowid < lowest_parked:
                lowest_parked = rowid
            continue
        max_processed = max(max_processed, rowid)
        if result.get("_skip"):
            continue
        if not result.get("_created", True):
            # Already on disk from a prior pass — don't re-emit.
            continue
        payload = {k: v for k, v in result.items() if not k.startswith("_")}
        payload = {"source": "macmail", "kind": "new_email", **payload}
        P.emit_event(payload)
        print(f"[macmail-in] emitted rowid={rowid} → {result['account']} ({result['direction']})", flush=True)

    new_last = min(lowest_parked - 1, max_processed) if lowest_parked is not None else max_processed
    new_last = max(new_last, last_rowid)
    if new_last != last_rowid:
        _save_cursor(new_last)
    return new_last


def _drain_catchup(last_rowid: int, cfg: A.AccountsConfig) -> int:
    """Boot-time pass — coalesce all missed mail into ONE backlog event so
    PAI gets a single nudge instead of N."""
    rows = _query_rows(last_rowid, cfg)
    if rows is None:
        return last_rowid
    if not rows:
        return last_rowid

    print(f"[macmail-in] catchup: {len(rows)} rows since rowid={last_rowid}", flush=True)
    summaries: dict[str, dict] = {}
    earliest: Optional[datetime] = None
    lowest_parked: Optional[int] = None
    max_processed = last_rowid
    for row in rows:
        rowid = int(row["rowid"])
        result = ingest_row(row, cfg)
        if result is None:
            if lowest_parked is None or rowid < lowest_parked:
                lowest_parked = rowid
            continue
        max_processed = max(max_processed, rowid)
        if result.get("_skip"):
            continue
        if not result.get("_created", True):
            # Already ingested in a prior boot — don't double-count in backlog.
            continue
        acc = result["account"]
        bucket = summaries.setdefault(acc, {"account": acc, "count": 0, "last_subject": ""})
        bucket["count"] += 1
        bucket["last_subject"] = result["subject"]
        ts = _mac_date_to_dt(int(row["date_received"] or 0))
        if earliest is None or ts < earliest:
            earliest = ts

    if summaries:
        total = sum(b["count"] for b in summaries.values())
        P.emit_event({
            "source": "macmail",
            "kind": "email_backlog",
            "since": earliest.isoformat(timespec="seconds") if earliest else None,
            "accounts": list(summaries.values()),
            "total": total,
        })
        print(f"[macmail-in] emitted backlog (total={total}, accounts={len(summaries)})", flush=True)

    new_last = min(lowest_parked - 1, max_processed) if lowest_parked is not None else max_processed
    new_last = max(new_last, last_rowid)
    if new_last != last_rowid:
        _save_cursor(new_last)
    if lowest_parked is not None:
        print(f"[macmail-in] catchup parked at rowid={lowest_parked} (partial emlx); will retry", flush=True)
    return new_last


# ---------- kqueue watcher (lifted from imessage/inbound.py) ---------------

WATCHED_NAMES = ("Envelope Index-wal",)

VNODE_FLAGS = (
    select.KQ_NOTE_WRITE
    | select.KQ_NOTE_EXTEND
    | select.KQ_NOTE_DELETE
    | select.KQ_NOTE_RENAME
)


class _KqueueWatcher:
    def __init__(self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue):
        self.loop = loop
        self.queue = queue
        self._kq: Optional[select.kqueue] = None
        self._fds: dict[int, str] = {}
        self._thread: Optional[threading.Thread] = None
        self._stop_fd_r: Optional[int] = None
        self._stop_fd_w: Optional[int] = None

    def _open_target(self, name: str) -> Optional[int]:
        path = ENVELOPE_DIR / name
        try:
            return os.open(str(path), os.O_RDONLY)
        except FileNotFoundError:
            return None

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
                print(f"[macmail-in] kqueue watching {name} fd={fd}", flush=True)
            else:
                print(f"[macmail-in] kqueue target {name} missing — skipping", flush=True)

        self._thread = threading.Thread(target=self._loop, name="macmail-in-kq", daemon=True)
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
            print(f"[macmail-in] kqueue re-opened {name} fd={fd}", flush=True)

    def _loop(self) -> None:
        assert self._kq is not None
        assert self._stop_fd_r is not None
        try:
            while True:
                events = self._kq.control([], 16, None)
                stop = False
                for ev in events:
                    if ev.ident == self._stop_fd_r:
                        stop = True
                        continue
                    fd = ev.ident
                    name = self._fds.get(fd, f"fd={fd}")
                    fflags = ev.fflags
                    self.loop.call_soon_threadsafe(self.queue.put_nowait, None)
                    if fflags & (select.KQ_NOTE_DELETE | select.KQ_NOTE_RENAME):
                        print(f"[macmail-in] kq {name} rotated, reopening", flush=True)
                        self._reopen(fd, name)
                if stop:
                    return
        except Exception as e:
            print(f"[macmail-in] kq-loop crashed: {e!r}", flush=True)

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
    if not ENVELOPE_INDEX.exists():
        print(f"[macmail-in] Envelope Index not found at {ENVELOPE_INDEX}; driver idle", flush=True)
        return

    cfg = await A.refresh()
    print(f"[macmail-in] discovered accounts: {A.summarize(cfg)}", flush=True)

    last_rowid = _load_cursor()
    if last_rowid is None:
        try:
            last_rowid = _bootstrap_cursor()
            print(f"[macmail-in] bootstrap cursor last_rowid={last_rowid}", flush=True)
        except sqlite3.OperationalError as e:
            print(f"[macmail-in] cannot bootstrap (FDA granted?): {e}", flush=True)
            return

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    watcher = _KqueueWatcher(loop, queue)
    watcher.start()
    print(f"[macmail-in] started, last_rowid={last_rowid}", flush=True)

    last_rowid = await asyncio.to_thread(_drain_catchup, last_rowid, cfg)

    # Periodic safety-net poll: kqueue catches WAL writes, but a row stuck on
    # `.partial.emlx` only unparks when Mail finishes the download — which may
    # or may not touch the WAL again before some unrelated activity does. A
    # cheap 60s tick guarantees we retry parked rows in bounded time.
    # The same tick refreshes the AppleScript-derived accounts config so
    # newly-added Mail.app accounts are picked up without a kernel restart.
    POLL_INTERVAL = 60.0
    ACCOUNTS_REFRESH_EVERY = 60  # ticks → 1h

    async def _ticker() -> None:
        while True:
            await asyncio.sleep(POLL_INTERVAL)
            queue.put_nowait(None)

    ticker_task = asyncio.create_task(_ticker())
    ticks = 0

    try:
        while True:
            await queue.get()
            while not queue.empty():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            ticks += 1
            if ticks % ACCOUNTS_REFRESH_EVERY == 0:
                fresh = await A.refresh()
                if fresh.accounts and fresh.accounts != cfg.accounts:
                    print(f"[macmail-in] accounts refreshed: {A.summarize(fresh)}", flush=True)
                    cfg = fresh
            last_rowid = await asyncio.to_thread(_drain_live, last_rowid, cfg)
    except asyncio.CancelledError:
        raise
    finally:
        ticker_task.cancel()
        watcher.stop()
        print("[macmail-in] stopped", flush=True)
