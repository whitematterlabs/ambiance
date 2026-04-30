"""macOS Mail.app outbound driver — drafts only (v1).

Watches home/communication/email/{account}/drafts/*.yaml. When PAI writes
a draft, this driver hands it to Mail.app via AppleScript `save` (NOT
`send`) — the draft lands in Mail's Drafts folder and Arda reviews +
sends manually.

v1 deliberately does not autosend. Even a hallucinated recipient or
content can't leave the machine without a human click. Autosend is a v2
problem.

Idempotency: each yaml gains `mail_app_drafted: true` once Mail.app has
acknowledged the AppleScript. We never re-process a marked file. So
boot-time scan + watchdog file events are equivalent — both just trigger
"look at this file, draft it if not already drafted".
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


EMAIL_ROOT = paths.var_spool_email()


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
    """A file we own: under {EMAIL_ROOT}/{account}/drafts/*.yaml."""
    if path.suffix != ".yaml":
        return False
    if path.name.endswith(".tmp"):
        return False
    try:
        rel = path.resolve().relative_to(EMAIL_ROOT.resolve())
    except (ValueError, OSError):
        return False
    parts = rel.parts
    # {account}/drafts/{name}.yaml
    return len(parts) == 3 and parts[1] == "drafts"


def _account_from_path(path: Path) -> str:
    return path.resolve().relative_to(EMAIL_ROOT.resolve()).parts[0]


# ---------- AppleScript ----------------------------------------------------

def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _build_new_message_script(draft: dict) -> str:
    subject = _esc(str(draft.get("subject") or ""))
    content = _esc(str(draft.get("content") or ""))
    to_list = [_esc(a) for a in (draft.get("to") or []) if a]
    cc_list = [_esc(a) for a in (draft.get("cc") or []) if a]
    bcc_list = [_esc(a) for a in (draft.get("bcc") or []) if a]

    recipients = []
    for a in to_list:
        recipients.append(f'  make new to recipient at end of to recipients with properties {{address:"{a}"}}')
    for a in cc_list:
        recipients.append(f'  make new cc recipient at end of cc recipients with properties {{address:"{a}"}}')
    for a in bcc_list:
        recipients.append(f'  make new bcc recipient at end of bcc recipients with properties {{address:"{a}"}}')
    recipients_block = "\n".join(recipients)

    return (
        'tell application "Mail"\n'
        '  set newMsg to make new outgoing message with properties '
        f'{{subject:"{subject}", content:"{content}", visible:false}}\n'
        '  tell newMsg\n'
        f'{recipients_block}\n'
        '    save\n'
        '  end tell\n'
        'end tell'
    )


def _build_reply_script(draft: dict) -> str:
    """Reply-shaped draft. Locates the parent by Message-ID and uses Mail's
    `reply` to inherit threading + recipients, then saves to Drafts."""
    parent = _esc(str(draft.get("in_reply_to") or ""))
    content = _esc(str(draft.get("content") or ""))
    return (
        'tell application "Mail"\n'
        '  set parentMsgs to {}\n'
        '  repeat with mb in (every mailbox)\n'
        '    try\n'
        f'      set parentMsgs to (messages of mb whose message id is "{parent}")\n'
        '      if (count of parentMsgs) > 0 then exit repeat\n'
        '    end try\n'
        '  end repeat\n'
        '  if (count of parentMsgs) is 0 then\n'
        '    error "parent message not found"\n'
        '  end if\n'
        '  set parentMsg to item 1 of parentMsgs\n'
        '  set replyMsg to reply parentMsg with opens window\n'
        '  delay 0.2\n'
        f'  set content of replyMsg to "{content}"\n'
        '  tell replyMsg to save\n'
        '  close (every window whose name contains "Re:")\n'
        'end tell'
    )


async def _run_osascript(script: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    return (
        proc.returncode if proc.returncode is not None else -1,
        stderr.decode("utf-8", errors="replace").strip(),
    )


# ---------- driver core ----------------------------------------------------

def _emit_failed(account: str, path: Path, reason: str) -> None:
    P.emit_event({
        "source": "macmail-out",
        "kind": "draft_failed",
        "account": account,
        "path": str(path.relative_to(paths.PAI_ROOT)) if path.is_absolute() else str(path),
        "reason": reason,
    })


async def _process(path: Path) -> None:
    if not _is_draft_path(path):
        return
    if not path.exists():
        return
    draft = _load(path)
    if draft is None:
        return
    if draft.get("mail_app_drafted"):
        return
    if not draft.get("to") and not draft.get("in_reply_to"):
        # Nothing actionable yet (PAI may still be writing).
        return

    account = _account_from_path(path)
    if draft.get("in_reply_to"):
        script = _build_reply_script(draft)
    else:
        script = _build_new_message_script(draft)

    code, err = await _run_osascript(script)
    if code != 0:
        reason = err or f"osascript exit {code}"
        print(f"[macmail-out] draft failed ({account}/{path.name}): {reason}", flush=True)
        _emit_failed(account, path, reason)
        # Mark so we don't loop forever on a permanently-bad draft.
        draft["mail_app_drafted"] = False
        draft["draft_error"] = reason
        draft["drafted_at"] = datetime.now().isoformat(timespec="seconds")
        _atomic_dump(path, draft)
        return

    draft["mail_app_drafted"] = True
    draft.pop("draft_error", None)
    draft["drafted_at"] = datetime.now().isoformat(timespec="seconds")
    _atomic_dump(path, draft)
    print(f"[macmail-out] drafted to Mail.app: {account}/{path.name}", flush=True)


def _scan_existing() -> list[Path]:
    if not EMAIL_ROOT.exists():
        return []
    out: list[Path] = []
    for account_dir in EMAIL_ROOT.iterdir():
        drafts = account_dir / "drafts"
        if not drafts.is_dir():
            continue
        for f in drafts.glob("*.yaml"):
            if _is_draft_path(f):
                out.append(f)
    return out


# ---------- watchdog plumbing ---------------------------------------------

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


async def run() -> None:
    EMAIL_ROOT.mkdir(parents=True, exist_ok=True)
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[Path] = asyncio.Queue()

    observer = Observer()
    observer.schedule(_Handler(loop, queue), str(EMAIL_ROOT), recursive=True)
    observer.start()
    print(f"[macmail-out] watching {EMAIL_ROOT}", flush=True)

    # Boot scan: any drafts already sitting around get re-evaluated.
    # Idempotent — already-drafted ones get skipped on the marker check.
    for f in _scan_existing():
        await _process(f)

    try:
        while True:
            path = await queue.get()
            # Coalesce bursts of write events for the same file.
            seen = {path}
            while not queue.empty():
                try:
                    seen.add(queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            for f in seen:
                await _process(f)
    except asyncio.CancelledError:
        raise
    finally:
        observer.stop()
        observer.join(timeout=2)
        print("[macmail-out] stopped", flush=True)
