"""macOS Mail.app outbound driver — drafts only (v1).

Watches `var/spool/communication/email/drafts/*.yaml` (single shared dir,
not per-account). Each draft yaml carries a required `from:` field naming
the Mail.app account that should own the draft. When PAI writes a draft,
this driver hands it to Mail.app via AppleScript `save` (NOT `send`) —
the draft lands in Mail's Drafts folder under the right account and Arda
reviews + sends manually.

v1 deliberately does not autosend. Even a hallucinated recipient or
content can't leave the machine without a human click. Autosend is a v2
problem.

Lifecycle (`draft_state` field):
  - missing / "pending"        → re-evaluate on next event
  - "pending_parent"           → reply parent not found yet; retry with backoff
  - "drafted"                  → terminal success; saved to Mail's Drafts
  - "failed"                   → terminal failure; draft_error explains why

Boot-time scan and watchdog events are equivalent — both trigger
"look at this file, draft it if it has no terminal state yet".
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

from . import accounts as A


EMAIL_ROOT = paths.var_spool_email()
DRAFTS_DIR = paths.var_spool_email_drafts()


# Reply retry schedule (seconds). Mail may not have synced the parent
# message yet; back off and try again.
REPLY_RETRY_DELAYS = (5.0, 15.0, 30.0)
PARENT_NOT_FOUND_MARKER = "parent message not found"
ACCOUNTS_REFRESH_INTERVAL = 3600.0  # 1h — handles account-add without restart


# ---------- yaml read/write -----------------------------------------------

class _LoadError(Exception):
    """Draft yaml is unreadable or malformed."""


def _load(path: Path) -> Optional[dict]:
    try:
        with path.open() as f:
            return yaml.safe_load(f) or {}
    except OSError:
        return None
    except yaml.YAMLError as e:
        raise _LoadError(f"yaml parse error: {e}") from e


def _atomic_dump(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
    os.replace(tmp, path)


def _is_draft_path(path: Path) -> bool:
    """A file we own: under {EMAIL_ROOT}/drafts/*.yaml (top-level, single dir)."""
    if path.suffix != ".yaml":
        return False
    if path.name.endswith(".tmp"):
        return False
    try:
        rel = path.resolve().relative_to(EMAIL_ROOT.resolve())
    except (ValueError, OSError):
        return False
    parts = rel.parts
    # drafts/{name}.yaml
    return len(parts) == 2 and parts[0] == "drafts"


# ---------- AppleScript ----------------------------------------------------

def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _build_new_message_script(account: str, draft: dict) -> str:
    sender = _esc(account)
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

    # `sender` pins which Mail account owns the draft. Without it, Mail
    # falls back to the default account regardless of where the yaml lives.
    return (
        'tell application "Mail"\n'
        '  set newMsg to make new outgoing message with properties '
        f'{{sender:"{sender}", subject:"{subject}", content:"{content}", visible:false}}\n'
        '  tell newMsg\n'
        f'{recipients_block}\n'
        '    save\n'
        '  end tell\n'
        'end tell'
    )


def _build_reply_script(account: str, draft: dict) -> str:
    """Reply-shaped draft. Locates the parent by Message-ID and uses Mail's
    `reply` to inherit threading + recipients, then saves to Drafts.

    Note: macOS 15 (Sequoia) dropped the `opens window` parameter on `reply`
    — earlier versions accepted `without opens window` to keep the reply
    hidden, but that now errors with -2741. Reply window will briefly flash;
    we close it after `save`.
    """
    sender = _esc(account)
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
        '  set replyMsg to reply parentMsg\n'
        f'  set sender of replyMsg to "{sender}"\n'
        f'  set content of replyMsg to "{content}"\n'
        '  save replyMsg\n'
        '  try\n'
        '    close (every window whose name starts with "Re:")\n'
        '  end try\n'
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


# ---------- Mail.app account validation ------------------------------------

# AppleScript-derived account config (single source of truth, shared with
# macmail-in via `accounts.yaml`). Refreshed at boot + hourly; consulted by
# `_process` to fail-fast on a `from:` that doesn't match any configured
# Mail.app address (including legitimate iCloud Hide-My-Email aliases).
# Initialized at the top of `run()`.
_accounts_cfg: Optional["A.AccountsConfig"] = None


# ---------- driver core ----------------------------------------------------

def _emit_failed(account: str, path: Path, reason: str) -> None:
    P.emit_event({
        "source": "macmail-out",
        "kind": "draft_failed",
        "account": account,
        "path": str(path.relative_to(paths.PAI_ROOT)) if path.is_absolute() else str(path),
        "reason": reason,
    })


def _mark_failed(path: Path, draft: dict, account: str, reason: str) -> None:
    draft["draft_state"] = "failed"
    draft["draft_error"] = reason
    draft["drafted_at"] = datetime.now().isoformat(timespec="seconds")
    _atomic_dump(path, draft)
    _emit_failed(account, path, reason)
    print(f"[macmail-out] draft failed ({account or '?'}/{path.name}): {reason}", flush=True)


async def _process(path: Path) -> None:
    if not _is_draft_path(path):
        return
    if not path.exists():
        return
    try:
        draft = _load(path)
    except _LoadError as e:
        # Common cause: PAI wrote `subject: Re: foo` (unquoted — the `: `
        # makes YAML parse it as a nested mapping). Don't rewrite the file
        # (we'd clobber the user's content), but log + emit so this never
        # silently no-ops again.
        reason = str(e)
        print(f"[macmail-out] draft unreadable ({path.name}): {reason}", flush=True)
        _emit_failed("", path, reason)
        return
    if draft is None:
        return
    # `draft_state`: "drafted" / "failed" are terminal — never re-process.
    # "pending_parent" is transient; the retry timer re-enqueues it.
    state = draft.get("draft_state")
    if state in ("drafted", "failed"):
        return
    if not draft.get("to") and not draft.get("in_reply_to"):
        # Nothing actionable yet (PAI may still be writing).
        return

    account = str(draft.get("from") or "").strip().lower()
    if not account:
        _mark_failed(path, draft, "", "draft is missing required `from:` field")
        return
    # Empty / unset config (AppleScript discovery failed or `_process`
    # invoked before `run()` initialized) → validation disabled, so we
    # don't reject every draft when Mail.app is briefly unavailable.
    cfg = _accounts_cfg
    if cfg is not None and not cfg.is_empty() and not cfg.accepts_from(account):
        known = ", ".join(cfg.all_addresses()) or "<none>"
        _mark_failed(
            path, draft, account,
            f"no Mail.app account for from: {account}; known: {known}",
        )
        return

    if draft.get("in_reply_to"):
        script = _build_reply_script(account, draft)
    else:
        script = _build_new_message_script(account, draft)

    code, err = await _run_osascript(script)
    if code != 0:
        reason = err or f"osascript exit {code}"
        # Reply parent may not be synced yet — retry with backoff before
        # marking terminal.
        if PARENT_NOT_FOUND_MARKER in reason and draft.get("in_reply_to"):
            retries = int(draft.get("draft_retries") or 0)
            if retries < len(REPLY_RETRY_DELAYS):
                delay = REPLY_RETRY_DELAYS[retries]
                draft["draft_state"] = "pending_parent"
                draft["draft_retries"] = retries + 1
                draft["draft_error"] = reason
                _atomic_dump(path, draft)
                print(
                    f"[macmail-out] reply parent not found; retry {retries + 1}/"
                    f"{len(REPLY_RETRY_DELAYS)} in {delay}s ({account}/{path.name})",
                    flush=True,
                )
                _schedule_retry(path, delay)
                return
        _mark_failed(path, draft, account, reason)
        return

    draft["draft_state"] = "drafted"
    draft.pop("draft_error", None)
    draft.pop("draft_retries", None)
    draft["drafted_at"] = datetime.now().isoformat(timespec="seconds")
    _atomic_dump(path, draft)
    print(f"[macmail-out] drafted to Mail.app: {account}/{path.name}", flush=True)


# ---------- retry scheduling ----------------------------------------------

# Set in `run()`; used by `_schedule_retry` to push the path back onto the
# queue after a delay without blocking the main loop.
_loop: Optional[asyncio.AbstractEventLoop] = None
_queue: Optional[asyncio.Queue] = None


def _schedule_retry(path: Path, delay: float) -> None:
    if _loop is None or _queue is None:
        return
    _loop.call_later(delay, _queue.put_nowait, path)


def _scan_existing() -> list[Path]:
    if not DRAFTS_DIR.exists():
        return []
    return [f for f in DRAFTS_DIR.glob("*.yaml") if _is_draft_path(f)]


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
    global _loop, _queue, _accounts_cfg

    DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[Path] = asyncio.Queue()
    _loop = loop
    _queue = queue

    _accounts_cfg = await A.refresh()
    if not _accounts_cfg.is_empty():
        print(f"[macmail-out] known Mail.app accounts: {_accounts_cfg.all_addresses()}", flush=True)
    else:
        print("[macmail-out] no Mail.app accounts enumerated; from: validation disabled", flush=True)

    async def _refresh_accounts() -> None:
        global _accounts_cfg
        while True:
            await asyncio.sleep(ACCOUNTS_REFRESH_INTERVAL)
            fresh = await A.refresh()
            if not fresh.is_empty() and fresh.accounts != _accounts_cfg.accounts:
                print(f"[macmail-out] account list changed: {fresh.all_addresses()}", flush=True)
                _accounts_cfg = fresh

    refresh_task = asyncio.create_task(_refresh_accounts())

    observer = Observer()
    observer.schedule(_Handler(loop, queue), str(DRAFTS_DIR), recursive=False)
    observer.start()
    print(f"[macmail-out] watching {DRAFTS_DIR}", flush=True)

    # Boot scan: any drafts already sitting around get re-evaluated.
    # Idempotent — terminal-state drafts get skipped on the marker check.
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
        refresh_task.cancel()
        observer.stop()
        observer.join(timeout=2)
        _loop = None
        _queue = None
        print("[macmail-out] stopped", flush=True)
