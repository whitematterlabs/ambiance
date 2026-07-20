"""Maildir inbound driver.

Watches `var/mail/<account>/INBOX/new/` for every configured account.
On each file landing, translates the RFC 5322 file into a canonical
yaml under `var/spool/communication/email/<account>/...` and emits a
`new_email` event.

Boot-time backlog: scans every INBOX/new/ against
`sys/drivers/maildir/cursors/<account>.yaml` (set of already-processed
Maildir filenames). Anything new is coalesced into one `email_backlog`
event. Cursor is updated as files are ingested.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from boot import paths
from boot import processes as P

from . import config as Cfg
from .ingest import ingest_file


def _cursor_dir() -> Path:
    return paths.sys_drivers() / "maildir" / "cursors"


def _cursor_path(account_address: str) -> Path:
    return _cursor_dir() / f"{account_address}.yaml"


def _load_cursor(account_address: str) -> set[str]:
    path = _cursor_path(account_address)
    if not path.exists():
        return set()
    with path.open() as f:
        data = yaml.safe_load(f) or {}
    return set(data.get("seen") or [])


def _save_cursor(account_address: str, seen: set[str]) -> None:
    _cursor_dir().mkdir(parents=True, exist_ok=True)
    path = _cursor_path(account_address)
    tmp = path.with_suffix(".yaml.tmp")
    # Cap retained set so the file doesn't grow unbounded. 5k filenames is
    # comfortably more than any realistic backlog window.
    trimmed = sorted(seen)[-5000:]
    with tmp.open("w") as f:
        yaml.safe_dump({"seen": trimmed}, f, sort_keys=False)
    os.replace(tmp, path)


def _inbox_new(account: Cfg.Account) -> Path:
    return account.maildir / "INBOX" / "new"


def _list_new_files(account: Cfg.Account) -> list[Path]:
    new_dir = _inbox_new(account)
    if not new_dir.is_dir():
        return []
    return sorted(p for p in new_dir.iterdir() if p.is_file())


def _process_file(path: Path, account: Cfg.Account) -> Optional[dict]:
    """Ingest a single file. Returns event payload or None on failure."""
    return ingest_file(path, account, direction="inbound")


# ---------- watchdog plumbing ---------------------------------------------

class _Handler(FileSystemEventHandler):
    """Fires for any file event under a watched INBOX/new/ tree."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        queue: asyncio.Queue[tuple[Path, str]],
        account_address: str,
    ):
        self.loop = loop
        self.queue = queue
        self.account = account_address

    def _enqueue(self, raw: str) -> None:
        p = Path(raw)
        # Maildir convention: filenames in new/ have no extension; mbsync
        # writes them atomically via tmp/ → new/ rename.
        if p.parent.name != "new":
            return
        self.loop.call_soon_threadsafe(
            self.queue.put_nowait, (p, self.account)
        )

    def on_created(self, event) -> None:  # type: ignore[override]
        if not event.is_directory:
            self._enqueue(event.src_path)

    def on_moved(self, event) -> None:  # type: ignore[override]
        if event.is_directory:
            return
        dest = getattr(event, "dest_path", None)
        if dest:
            self._enqueue(dest)


# ---------- boot backlog --------------------------------------------------

def _drain_backlog(accounts: list[Cfg.Account]) -> Optional[dict]:
    """Per-account boot scan. Coalesces all new files into one backlog
    event payload. Updates cursors as it goes.

    Returns the email_backlog payload, or None if nothing was new.
    """
    summaries: dict[str, dict] = {}
    earliest: Optional[datetime] = None
    total = 0
    for account in accounts:
        seen = _load_cursor(account.address)
        new_files = _list_new_files(account)
        ingested_any = False
        for f in new_files:
            if f.name in seen:
                continue
            result = _process_file(f, account)
            seen.add(f.name)
            ingested_any = True
            if result is None:
                continue
            bucket = summaries.setdefault(
                account.address,
                {"account": account.address, "count": 0, "last_subject": ""},
            )
            bucket["count"] += 1
            bucket["last_subject"] = result["subject"]
            total += 1
            try:
                ts = datetime.fromtimestamp(f.stat().st_mtime).astimezone()
            except OSError:
                ts = None
            if ts and (earliest is None or ts < earliest):
                earliest = ts
        if ingested_any:
            _save_cursor(account.address, seen)

    if total == 0:
        return None
    return {
        "source": "maildir",
        "kind": "email_backlog",
        "since": earliest.isoformat(timespec="seconds") if earliest else None,
        "accounts": list(summaries.values()),
        "total": total,
    }


# ---------- live drain ----------------------------------------------------

def _process_live(path: Path, account: Cfg.Account) -> None:
    seen = _load_cursor(account.address)
    if path.name in seen:
        return
    if not path.exists():
        return
    result = _process_file(path, account)
    seen.add(path.name)
    _save_cursor(account.address, seen)
    if result is None:
        return
    P.emit_event({"source": "maildir", "kind": "new_email", **result})
    print(
        f"[maildir-in] emitted {account.address} → {result['subject']!r}",
        flush=True,
    )


# ---------- run loop ------------------------------------------------------

async def run() -> None:
    accounts = Cfg.load_accounts()
    if not accounts:
        print("[maildir-in] no accounts configured; idle", flush=True)
        # Sleep forever — kernel will cancel on shutdown.
        await asyncio.Event().wait()
        return

    # Ensure the Maildir tree exists for each account so watchdog has
    # something to watch. mbsync also creates these but we don't want a
    # race with first run.
    for a in accounts:
        for sub in ("INBOX/new", "INBOX/cur", "INBOX/tmp"):
            (a.maildir / sub).mkdir(parents=True, exist_ok=True)

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[Path, str]] = asyncio.Queue()
    addr_to_account = {a.address: a for a in accounts}

    observer = Observer()
    for a in accounts:
        observer.schedule(
            _Handler(loop, queue, a.address),
            str(_inbox_new(a)),
            recursive=False,
        )
    observer.start()
    print(
        f"[maildir-in] watching {len(accounts)} account(s): "
        f"{', '.join(a.address for a in accounts)}",
        flush=True,
    )

    backlog = await asyncio.to_thread(_drain_backlog, accounts)
    if backlog is not None:
        P.emit_event(backlog)
        print(
            f"[maildir-in] emitted backlog (total={backlog['total']}, "
            f"accounts={len(backlog['accounts'])})",
            flush=True,
        )

    try:
        while True:
            path, address = await queue.get()
            account = addr_to_account.get(address)
            if account is None:
                continue
            await asyncio.to_thread(_process_live, path, account)
    except asyncio.CancelledError:
        raise
    finally:
        observer.stop()
        observer.join(timeout=2)
        print("[maildir-in] stopped", flush=True)
