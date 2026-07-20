"""Maildir outbound driver — send via msmtp.

Watches `var/spool/communication/email/<account>/drafts/*.yaml`. When PAI
writes a draft, this driver:

  1. Renders an RFC 5322 message (with our own Message-ID, Date, threading
     headers).
  2. Pipes it to `msmtp -a <account> -t` to deliver via SMTP.
  3. On success: writes the outbound canonical YAML, drops a copy in the
     local Maildir Sent/new/ for mbsync to push to the server, and stamps
     the draft as `sent: true`.
  4. On failure: emits `email:send_failed` and stamps the draft with the
     error so we don't loop on a permanently-bad recipient.

This is autosend: drafts authored by PAI go out as soon as they appear in
the drafts directory and have a `to:` set. Idempotency is on the `sent:`
flag stamped onto the yaml after a successful msmtp call.
"""

from __future__ import annotations

import asyncio
import email.utils
import os
import socket
import uuid
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Optional

import yaml
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from boot import paths
from boot import processes as P

from .. import shared
from . import config as Cfg


def _email_root() -> Path:
    return paths.var_spool_email()


# ---------- yaml read/write -----------------------------------------------

def _load(path: Path) -> Optional[dict]:
    try:
        with path.open() as f:
            return yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return None


def _atomic_dump(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
    os.replace(tmp, path)


def _is_draft_path(path: Path) -> bool:
    if path.suffix != ".yaml":
        return False
    if path.name.endswith(".tmp"):
        return False
    try:
        rel = path.resolve().relative_to(_email_root().resolve())
    except (ValueError, OSError):
        return False
    parts = rel.parts
    return len(parts) == 3 and parts[1] == "drafts"


def _account_from_path(path: Path) -> str:
    return path.resolve().relative_to(_email_root().resolve()).parts[0]


# ---------- threading helpers ---------------------------------------------

def _parent_headers(account_dir: Path, in_reply_to: str) -> tuple[str, list[str]]:
    """Look up the parent yaml by Message-ID, return (in_reply_to, references)
    populated from the parent's threading. Falls back to (in_reply_to, []).
    """
    parent_path = shared.find_message_by_id(account_dir, in_reply_to)
    if parent_path is None:
        return in_reply_to, [in_reply_to]
    try:
        with parent_path.open() as f:
            parent = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return in_reply_to, [in_reply_to]
    refs = list(parent.get("references") or [])
    parent_id = (parent.get("message_id") or in_reply_to).strip()
    if parent_id and parent_id not in refs:
        refs.append(parent_id)
    return in_reply_to, refs


# ---------- RFC 5322 build ------------------------------------------------

def _new_message_id(domain: str) -> str:
    return f"<{uuid.uuid4().hex}@{domain or socket.getfqdn() or 'localhost'}>"


def _render(draft: dict, account: Cfg.Account) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = account.address
    to = draft.get("to") or []
    cc = draft.get("cc") or []
    bcc = draft.get("bcc") or []
    if to:
        msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    if bcc:
        msg["Bcc"] = ", ".join(bcc)
    msg["Subject"] = str(draft.get("subject") or "")
    msg["Date"] = email.utils.format_datetime(datetime.now(timezone.utc).astimezone())
    domain = account.address.split("@", 1)[-1]
    msg["Message-ID"] = draft.get("message_id") or _new_message_id(domain)

    in_reply_to = (draft.get("in_reply_to") or "").strip()
    if in_reply_to:
        account_dir = _email_root() / account.address
        irt, refs = _parent_headers(account_dir, in_reply_to)
        msg["In-Reply-To"] = irt
        if refs:
            msg["References"] = " ".join(refs)

    msg.set_content(str(draft.get("content") or ""))
    return msg


# ---------- msmtp + Sent copy ---------------------------------------------

async def _msmtp_send(account: Cfg.Account, raw: bytes) -> tuple[int, str]:
    msmtprc = paths.etc_mail() / "msmtprc"
    proc = await asyncio.create_subprocess_exec(
        "msmtp",
        "-C", str(msmtprc),
        "-a", account.address,
        "-t",  # read recipients from To/Cc/Bcc
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate(input=raw)
    return (
        proc.returncode if proc.returncode is not None else -1,
        stderr.decode("utf-8", errors="replace").strip(),
    )


def _drop_in_sent(account: Cfg.Account, raw: bytes, message_id: str) -> Path:
    """Write a Maildir-spec file into Sent/new/ for mbsync to push.

    Maildir filename convention: <unixtime>.<unique>.<host>. Drop into
    tmp/ first then rename into new/ (atomic).
    """
    sent = account.maildir / "Sent"
    new = sent / "new"
    tmp = sent / "tmp"
    new.mkdir(parents=True, exist_ok=True)
    tmp.mkdir(parents=True, exist_ok=True)
    unique = message_id.strip("<>").replace("@", "_") or uuid.uuid4().hex
    name = f"{int(datetime.now().timestamp())}.{unique}.{socket.gethostname()}"
    tmp_path = tmp / name
    tmp_path.write_bytes(raw)
    final = new / name
    os.rename(tmp_path, final)
    return final


# ---------- canonical YAML write ------------------------------------------

def _write_outbound_yaml(account: Cfg.Account, draft: dict, msg: EmailMessage) -> Path:
    sent_at = datetime.now().astimezone()
    references = []
    refs_hdr = msg.get("References")
    if refs_hdr:
        references = [tok for tok in str(refs_hdr).split() if tok.startswith("<")]
    in_reply_to = (msg.get("In-Reply-To") or "").strip() or None
    message_id = (msg.get("Message-ID") or "").strip()
    subject = str(draft.get("subject") or "")
    body = str(draft.get("content") or "")

    msg_dict = {
        "message_id": message_id,
        "in_reply_to": in_reply_to,
        "references": references,
        "thread_slug": shared.thread_slug(subject, references, message_id),
        "from": account.address,
        "to": list(draft.get("to") or []),
        "cc": list(draft.get("cc") or []),
        "bcc": list(draft.get("bcc") or []),
        "subject": subject,
        "direction": "outbound",
        "content": body,
        "attachments": [],
        "sent_at": sent_at.isoformat(timespec="seconds"),
    }
    account_dir = _email_root() / account.address
    msg_path = shared.write_message_yaml(account_dir, msg_dict)
    shared.link_thread(account_dir, msg_path, msg_dict["thread_slug"], sent_at)
    if in_reply_to:
        parent_path = shared.find_message_by_id(account_dir, in_reply_to)
        if parent_path:
            shared.link_prev(msg_path, parent_path)
    return msg_path


# ---------- driver core ---------------------------------------------------

def _emit_failed(account_address: str, path: Path, reason: str) -> None:
    P.emit_event({
        "source": "maildir-out",
        "kind": "send_failed",
        "account": account_address,
        "path": str(path.relative_to(paths.PAI_ROOT)) if path.is_absolute() else str(path),
        "reason": reason,
    })


async def _process(path: Path, accounts: dict[str, Cfg.Account]) -> None:
    if not _is_draft_path(path):
        return
    if not path.exists():
        return
    draft = _load(path)
    if draft is None:
        return
    if draft.get("sent"):
        return
    if not draft.get("to") and not draft.get("in_reply_to"):
        return  # not actionable; PAI may still be writing

    address = _account_from_path(path)
    account = accounts.get(address)
    if account is None:
        reason = f"no account configured for {address}"
        print(f"[maildir-out] {reason} ({path.name})", flush=True)
        _emit_failed(address, path, reason)
        draft["sent"] = False
        draft["send_error"] = reason
        _atomic_dump(path, draft)
        return

    msg = _render(draft, account)
    raw = msg.as_bytes()

    code, err = await _msmtp_send(account, raw)
    if code != 0:
        reason = err or f"msmtp exit {code}"
        print(f"[maildir-out] send failed {address}/{path.name}: {reason}", flush=True)
        _emit_failed(address, path, reason)
        draft["sent"] = False
        draft["send_error"] = reason
        draft["attempted_at"] = datetime.now().isoformat(timespec="seconds")
        _atomic_dump(path, draft)
        return

    msg_path = _write_outbound_yaml(account, draft, msg)
    _drop_in_sent(account, raw, str(msg.get("Message-ID") or ""))

    draft["sent"] = True
    draft.pop("send_error", None)
    draft["sent_at"] = datetime.now().isoformat(timespec="seconds")
    draft["message_id"] = str(msg.get("Message-ID") or "")
    draft["canonical_path"] = str(msg_path.relative_to(paths.PAI_ROOT))
    _atomic_dump(path, draft)
    print(f"[maildir-out] sent {address}/{path.name}", flush=True)


def _scan_existing() -> list[Path]:
    root = _email_root()
    if not root.exists():
        return []
    out: list[Path] = []
    for account_dir in root.iterdir():
        drafts = account_dir / "drafts"
        if not drafts.is_dir():
            continue
        for f in drafts.glob("*.yaml"):
            if _is_draft_path(f):
                out.append(f)
    return out


# ---------- watchdog plumbing --------------------------------------------

class _Handler(FileSystemEventHandler):
    def __init__(self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue[Path]):
        self.loop = loop
        self.queue = queue

    def _enqueue(self, raw: str) -> None:
        p = Path(raw)
        if p.suffix == ".yaml":
            self.loop.call_soon_threadsafe(self.queue.put_nowait, p)

    def on_created(self, event) -> None:  # type: ignore[override]
        if not event.is_directory:
            self._enqueue(event.src_path)

    def on_modified(self, event) -> None:  # type: ignore[override]
        if not event.is_directory:
            self._enqueue(event.src_path)

    def on_moved(self, event) -> None:  # type: ignore[override]
        if event.is_directory:
            return
        dest = getattr(event, "dest_path", None)
        if dest:
            self._enqueue(dest)


# ---------- run loop ------------------------------------------------------

async def run() -> None:
    root = _email_root()
    root.mkdir(parents=True, exist_ok=True)

    accounts = {a.address: a for a in Cfg.load_accounts()}

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[Path] = asyncio.Queue()

    observer = Observer()
    observer.schedule(_Handler(loop, queue), str(root), recursive=True)
    observer.start()
    print(f"[maildir-out] watching {root}", flush=True)

    for f in _scan_existing():
        await _process(f, accounts)

    try:
        while True:
            path = await queue.get()
            seen = {path}
            while not queue.empty():
                try:
                    seen.add(queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            for f in seen:
                await _process(f, accounts)
    except asyncio.CancelledError:
        raise
    finally:
        observer.stop()
        observer.join(timeout=2)
        print("[maildir-out] stopped", flush=True)
