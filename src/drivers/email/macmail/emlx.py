"""emlx file format helpers.

Mail.app stores each message at:
    ~/Library/Mail/V10/{account-uuid}/{Mailbox}.mbox/{store-uuid}/Data/[{rowid//1000}/]Messages/{rowid}.emlx

The bucket subdir (`Data/{N}/`) is omitted when the bucket index is 0 — i.e.
ROWIDs 0–999 live directly under `Data/Messages/`.

`.emlx` framing:
    <byte-count><spaces?>\\n         # ASCII decimal length of the MIME blob
    <byte-count bytes of RFC 5322 MIME>
    <XML plist>                      # mail.app metadata, ignored

`.partial.emlx` = body not fully downloaded. Skip; the next WAL kick after
Mail finishes the download will surface the full file.
"""

from __future__ import annotations

import email
import email.policy
import email.utils
from email.message import Message
from pathlib import Path
from typing import Optional

from .. import shared

MAIL_ROOT = Path.home() / "Library" / "Mail" / "V10"


def emlx_path(account_uuid: str, mailbox_name: str, rowid: int) -> Optional[Path]:
    """Resolve the on-disk emlx for a given (account, mailbox, ROWID).

    Returns None if the file is missing or only the .partial.emlx variant
    exists — caller should leave the cursor parked and wait for the next
    WAL kick.
    """
    mbox_dir = MAIL_ROOT / account_uuid / f"{mailbox_name}.mbox"
    if not mbox_dir.is_dir():
        return None
    bucket = rowid // 1000
    bucket_part = "" if bucket == 0 else f"{bucket}/"
    # The store-uuid dir under the .mbox is opaque; there's typically one,
    # but we glob in case Mail has rotated stores.
    for store in mbox_dir.iterdir():
        if not store.is_dir():
            continue
        candidate = store / "Data" / f"{bucket_part}Messages" / f"{rowid}.emlx"
        if candidate.exists():
            return candidate
    return None


def parse_emlx(data: bytes) -> Message:
    """Strip the leading byte-count line and trailing plist, return a parsed
    email.message.Message. Uses the modern `default` policy so headers come
    back as decoded unicode strings."""
    nl = data.find(b"\n")
    if nl < 0:
        raise ValueError("emlx: missing length header")
    try:
        count = int(data[:nl].strip())
    except ValueError as e:
        raise ValueError(f"emlx: bad length header: {data[:nl]!r}") from e
    start = nl + 1
    end = start + count
    if end > len(data):
        raise ValueError("emlx: declared length exceeds file size")
    return email.message_from_bytes(data[start:end], policy=email.policy.default)


def extract_text(msg: Message) -> str:
    """Walk the MIME tree, prefer text/plain; fall back to html_to_text on
    text/html. Returns the body as a single string."""
    plain_parts: list[str] = []
    html_parts: list[str] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        # Ignore attachments — body parts only.
        disp = (part.get("Content-Disposition") or "").lower()
        if "attachment" in disp:
            continue
        if ctype == "text/plain":
            try:
                plain_parts.append(part.get_content())
            except Exception:
                payload = part.get_payload(decode=True) or b""
                plain_parts.append(payload.decode("utf-8", errors="replace"))
        elif ctype == "text/html":
            try:
                html_parts.append(part.get_content())
            except Exception:
                payload = part.get_payload(decode=True) or b""
                html_parts.append(payload.decode("utf-8", errors="replace"))
    if plain_parts:
        return "\n\n".join(p.strip() for p in plain_parts if p.strip()) + "\n"
    if html_parts:
        return shared.html_to_text("\n".join(html_parts))
    return ""


def extract_addresses(header_value: Optional[str]) -> list[dict]:
    """Parse a To/Cc/From-style header into [{address, name}] dicts."""
    if not header_value:
        return []
    out: list[dict] = []
    for name, addr in email.utils.getaddresses([header_value]):
        if not addr:
            continue
        entry: dict = {"address": addr.lower()}
        if name:
            entry["name"] = name
        out.append(entry)
    return out


def header_id_list(header_value: Optional[str]) -> list[str]:
    """Parse References/In-Reply-To into a list of bare Message-IDs (with
    angle brackets preserved — we keep the RFC form as canonical)."""
    if not header_value:
        return []
    # email.utils doesn't have a reference-list parser; do it by hand.
    # Message-IDs are <local@domain> tokens separated by whitespace.
    out: list[str] = []
    buf: list[str] = []
    depth = 0
    for ch in header_value:
        if ch == "<":
            depth = 1
            buf = ["<"]
        elif ch == ">" and depth:
            buf.append(">")
            out.append("".join(buf))
            depth = 0
            buf = []
        elif depth:
            buf.append(ch)
    return out
