"""Gmail inbound driver.

One supervised proc (`gmail-in`) fans out to per-account async loops. Each
loop polls `users.history.list` from a persisted `historyId`, fetches new
INBOX messages, writes per-message yaml under
`home/communication/email/{account}/{date}/...`, builds the threads/ and
.prev symlink indexes, and emits `new_email` / `email_backlog` events into
`home/events/`.

Bootstrap and cursor-reset both capture a fresh `historyId` via getProfile
and ingest nothing — never backfills, per EMAILS.md.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
from datetime import datetime, timezone
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Optional

import yaml

from drivers.email import shared
from drivers.email.gmail import api as gapi
from drivers.email.gmail import auth as gauth
from boot import processes as P

DRIVER_SLUG = "gmail-in"
EMAIL_ROOT = P.HOME_DIR / "communication" / "email"
TMP_ROOT = P.HOME_DIR / "tmp" / "drivers" / DRIVER_SLUG


# ---------- account discovery ----------


def _account_dirs() -> list[Path]:
    if not EMAIL_ROOT.exists():
        return []
    out: list[Path] = []
    for d in sorted(EMAIL_ROOT.iterdir()):
        meta = d / "meta.yaml"
        if not meta.exists():
            continue
        try:
            with meta.open() as f:
                data = yaml.safe_load(f) or {}
        except Exception:
            continue
        if data.get("provider") == "gmail":
            out.append(d)
    return out


def _load_meta(account_dir: Path) -> dict:
    with (account_dir / "meta.yaml").open() as f:
        return yaml.safe_load(f) or {}


def _account_tmp(account: str) -> Path:
    return TMP_ROOT / account


def _cursor_path(account: str) -> Path:
    return _account_tmp(account) / "history-id"


def _token_path(account: str) -> Path:
    return _account_tmp(account) / "token.json"


def _load_cursor(account: str) -> Optional[str]:
    p = _cursor_path(account)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
    except json.JSONDecodeError:
        return None
    val = data.get("historyId")
    return str(val) if val is not None else None


def _save_cursor(account: str, history_id: str) -> None:
    p = _cursor_path(account)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"historyId": str(history_id)}))
    os.replace(tmp, p)


# ---------- MIME parsing ----------


def _decode_b64url(data: str) -> bytes:
    # Gmail uses URL-safe base64, sometimes without padding.
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def _walk_parts(payload: dict) -> tuple[str, str]:
    """Return (mime_type_used, decoded_text). Prefer text/plain; fall back to
    text/html via shared.html_to_text."""
    plain: Optional[str] = None
    html: Optional[str] = None

    def visit(part: dict) -> None:
        nonlocal plain, html
        mime = part.get("mimeType", "")
        body = part.get("body") or {}
        data = body.get("data")
        if data and mime == "text/plain" and plain is None:
            try:
                plain = _decode_b64url(data).decode("utf-8", errors="replace")
            except Exception:
                pass
        elif data and mime == "text/html" and html is None:
            try:
                html = _decode_b64url(data).decode("utf-8", errors="replace")
            except Exception:
                pass
        for child in part.get("parts") or []:
            visit(child)

    visit(payload)
    if plain is not None:
        return "text/plain", plain
    if html is not None:
        return "text/html", shared.html_to_text(html)
    return "text/plain", ""


def _headers_dict(payload: dict) -> dict[str, str]:
    """Case-insensitive lookup. Last value wins on duplicates (rare)."""
    out: dict[str, str] = {}
    for h in payload.get("headers") or []:
        name = (h.get("name") or "").strip()
        if name:
            out[name.lower()] = h.get("value", "")
    return out


_REF_RE = re.compile(r"<[^<>]+>")


def _split_refs(value: str) -> list[str]:
    return _REF_RE.findall(value or "")


def _addresses(value: str) -> list[str]:
    return [addr for _, addr in getaddresses([value or ""]) if addr]


def _parse_received_at(date_header: str, internal_date_ms: Optional[str]) -> datetime:
    if date_header:
        try:
            dt = parsedate_to_datetime(date_header)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone()
        except (TypeError, ValueError):
            pass
    if internal_date_ms:
        try:
            return datetime.fromtimestamp(int(internal_date_ms) / 1000, tz=timezone.utc).astimezone()
        except (TypeError, ValueError):
            pass
    return datetime.now().astimezone()


def _build_message(account: str, gmail_msg: dict) -> dict:
    payload = gmail_msg.get("payload") or {}
    headers = _headers_dict(payload)
    from_raw = headers.get("from", "")
    from_name, from_addr = parseaddr(from_raw)
    received_at = _parse_received_at(headers.get("date", ""), gmail_msg.get("internalDate"))
    _, body_text = _walk_parts(payload)

    msg_id = headers.get("message-id", "").strip() or f"<gmail-{gmail_msg.get('id')}@noid>"
    refs = _split_refs(headers.get("references", ""))
    in_reply_to = (_split_refs(headers.get("in-reply-to", "")) or [None])[0]
    subject = headers.get("subject", "") or "(no subject)"

    return {
        "message_id": msg_id,
        "in_reply_to": in_reply_to,
        "references": refs,
        "thread_slug": shared.thread_slug(subject, refs, msg_id),
        "from": from_addr,
        "from_name": from_name or None,
        "to": _addresses(headers.get("to", "")),
        "cc": _addresses(headers.get("cc", "")),
        "bcc": _addresses(headers.get("bcc", "")),
        "subject": subject,
        "received_at": received_at.isoformat(timespec="seconds"),
        "direction": "inbound",
        "content": body_text,
        "attachments": [],
        "provider_thread_id": gmail_msg.get("threadId"),
    }


# ---------- ingest ----------


def _extract_added_message_ids(history: dict) -> list[str]:
    """Pull message ids from a history.list response, filtering to INBOX
    and excluding DRAFT/SENT. The history payload includes `messagesAdded`
    entries; each has a `message` with `id`, `threadId`, and `labelIds`."""
    out: list[str] = []
    seen: set[str] = set()
    for h in history.get("history") or []:
        for added in h.get("messagesAdded") or []:
            m = added.get("message") or {}
            mid = m.get("id")
            if not mid or mid in seen:
                continue
            labels = set(m.get("labelIds") or [])
            if "INBOX" not in labels:
                continue
            if labels & {"DRAFT", "SENT"}:
                continue
            seen.add(mid)
            out.append(mid)
    return out


def _ingest_one(account_dir: Path, account: str, gmail_msg: dict) -> Optional[dict]:
    """Write yaml + symlinks. Return event payload (without source/kind) or None."""
    msg = _build_message(account, gmail_msg)
    msg_path = shared.write_message_yaml(account_dir, msg)
    received_at = datetime.fromisoformat(msg["received_at"])
    shared.link_thread(account_dir, msg_path, msg["thread_slug"], received_at)

    parent_id = msg["in_reply_to"] or (msg["references"][-1] if msg["references"] else None)
    parent_path = shared.find_message_by_id(account_dir, parent_id) if parent_id else None
    shared.link_prev(msg_path, parent_path)

    rel_path = msg_path.relative_to(P.HOME_DIR.parent) if msg_path.is_absolute() else msg_path
    return {
        "account": account,
        "provider": "gmail",
        "thread_slug": msg["thread_slug"],
        "subject": msg["subject"],
        "from": msg["from"],
        "path": str(rel_path),
    }


# ---------- per-account loop ----------


async def _account_loop(account_dir: Path) -> None:
    meta = _load_meta(account_dir)
    account = meta["account"]
    poll = int(meta.get("poll_interval_seconds") or 60)
    token_path = _token_path(account)
    if not token_path.exists():
        P.append_log(DRIVER_SLUG, f"{account}: no token.json; run addemail")
        return

    creds = await asyncio.to_thread(gauth.load_credentials, token_path)

    cursor = _load_cursor(account)
    if cursor is None:
        # Bootstrap: capture current historyId, ingest nothing.
        prof = await asyncio.to_thread(gapi.get_profile, creds)
        cursor = str(prof["historyId"])
        _save_cursor(account, cursor)
        P.append_log(DRIVER_SLUG, f"{account}: bootstrap historyId={cursor}")
        bootstrap_just_ran = True
    else:
        bootstrap_just_ran = False

    P.append_log(DRIVER_SLUG, f"{account}: loop starting (poll={poll}s)")

    while True:
        await asyncio.sleep(poll)
        try:
            cursor = await _drain_once(account_dir, account, creds, cursor, bootstrap_just_ran)
            bootstrap_just_ran = False
        except gapi.HistoryIdInvalid:
            prof = await asyncio.to_thread(gapi.get_profile, creds)
            cursor = str(prof["historyId"])
            _save_cursor(account, cursor)
            P.emit_event({
                "source": "email",
                "kind": "email_cursor_reset",
                "account": account,
                "provider": "gmail",
            })
            P.append_log(DRIVER_SLUG, f"{account}: historyId expired; reset to {cursor}")
        except Exception as e:
            P.append_log(DRIVER_SLUG, f"{account}: poll error {e!r}")


async def _drain_once(
    account_dir: Path,
    account: str,
    creds,
    cursor: str,
    coalesce_backlog: bool,
) -> str:
    """One full drain from `cursor`. Returns the new cursor.

    If `coalesce_backlog` and N>1 messages land in one drain, emit a single
    `email_backlog` event instead of N `new_email` events (mirrors imessage).
    """
    new_cursor = cursor
    page_token: Optional[str] = None
    message_ids: list[str] = []
    while True:
        page = await asyncio.to_thread(gapi.history_list, creds, cursor, page_token)
        page_history = page.get("historyId")
        if page_history:
            new_cursor = str(page_history)
        message_ids.extend(_extract_added_message_ids(page))
        page_token = page.get("nextPageToken")
        if not page_token:
            break

    if not message_ids:
        if new_cursor != cursor:
            _save_cursor(account, new_cursor)
        return new_cursor

    events: list[dict] = []
    for mid in message_ids:
        try:
            full = await asyncio.to_thread(gapi.messages_get, creds, mid)
        except gapi.GmailApiError as e:
            P.append_log(DRIVER_SLUG, f"{account}: get {mid} failed {e!r}")
            continue
        try:
            ev = await asyncio.to_thread(_ingest_one, account_dir, account, full)
        except Exception as e:
            P.append_log(DRIVER_SLUG, f"{account}: ingest {mid} failed {e!r}")
            continue
        if ev:
            events.append(ev)

    if events:
        if coalesce_backlog and len(events) > 1:
            P.emit_event({
                "source": "email",
                "kind": "email_backlog",
                "account": account,
                "provider": "gmail",
                "messages": events,
            })
            P.append_log(DRIVER_SLUG, f"{account}: backlog ({len(events)} messages)")
        else:
            for ev in events:
                P.emit_event({"source": "email", "kind": "new_email", **ev})
            P.append_log(DRIVER_SLUG, f"{account}: emitted {len(events)} new_email")

    _save_cursor(account, new_cursor)
    return new_cursor


# ---------- entrypoint ----------


async def run() -> None:
    accounts = _account_dirs()
    if not accounts:
        print(f"[{DRIVER_SLUG}] no gmail accounts configured; idling", flush=True)
        # Stay alive so the supervised proc stays in `running`.
        while True:
            await asyncio.sleep(3600)

    print(f"[{DRIVER_SLUG}] starting {len(accounts)} account(s)", flush=True)
    tasks = [asyncio.create_task(_account_loop(d), name=f"gmail-in:{d.name}") for d in accounts]
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        for t in tasks:
            t.cancel()
        raise
