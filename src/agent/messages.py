"""Inbox spool messages — the v4 member-to-member wire.

A message is a file. Delivery is write-to-`tmp/`-then-rename-into-`in/`
(atomic, and the inotify watch fires once, on IN_MOVED_TO). Sender
identity is the file's OWNER UID — enforced by the kernel, no header to
trust or forge. Consumed messages move to `cur/`, maildir-style, so a
crash between read and turn loses nothing.

    /var/spool/pai/<member>/tmp/   staging (writers)
    /var/spool/pai/<member>/in/    unread — the watched wake edge
    /var/spool/pai/<member>/cur/   consumed archive
"""

from __future__ import annotations

import os
import pwd
import time
from dataclasses import dataclass
from pathlib import Path

from . import paths


@dataclass(frozen=True)
class Message:
    path: Path
    sender: str  # unix name of the file's owner
    ts: float  # mtime — delivery time
    body: str


def _owner_name(uid: int) -> str:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def read(path: Path) -> Message | None:
    """Load one spool file; None if it vanished (raced consumer) or is
    unreadable (never let one bad file wedge the inbox)."""
    try:
        st = path.stat()
        body = path.read_text(errors="replace")
    except OSError:
        return None
    return Message(path=path, sender=_owner_name(st.st_uid), ts=st.st_mtime, body=body)


def collect(inbox: Path) -> list[Message]:
    """Every unread message, oldest first."""
    try:
        entries = [p for p in inbox.iterdir() if p.is_file()]
    except OSError:
        return []
    out = [m for p in entries if (m := read(p)) is not None]
    out.sort(key=lambda m: (m.ts, m.path.name))
    return out


def archive(msg: Message) -> None:
    cur = msg.path.parent.parent / "cur"
    try:
        cur.mkdir(exist_ok=True)
        os.replace(msg.path, cur / msg.path.name)
    except OSError as e:
        print(f"agent: archive failed for {msg.path.name}: {e}", flush=True)


def deliver(to: str, body: str) -> Path:
    """Drop a message in another member's inbox. Raises OSError when the
    target spool doesn't exist or DAC refuses — callers surface that."""
    spool = paths.SPOOL / to
    tmp_dir = spool / "tmp"
    tmp_dir.mkdir(exist_ok=True)
    name = f"{time.time():.6f}.{os.getpid()}"
    staged = tmp_dir / name
    staged.write_text(body)
    final = spool / "in" / name
    os.replace(staged, final)
    return final
