"""RFC 5322 parsing helpers shared across email drivers.

Pure functions over `email.message.Message`. Address/header/body extraction
that any provider needs once the bytes are off-disk. Maildir files are raw
RFC 5322; emlx files are framed RFC 5322 (see drivers that handle them).
"""

from __future__ import annotations

import email
import email.policy
import email.utils
from email.message import Message
from typing import Optional

from . import shared


def parse_bytes(data: bytes) -> Message:
    """Parse a raw RFC 5322 byte blob (e.g. a Maildir file) under the modern
    `default` policy so headers come back as decoded unicode strings."""
    return email.message_from_bytes(data, policy=email.policy.default)


def extract_text(msg: Message) -> str:
    """Walk MIME, prefer text/plain, fall back to html_to_text on text/html."""
    plain_parts: list[str] = []
    html_parts: list[str] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
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
    """Parse References/In-Reply-To into a list of bare Message-IDs."""
    if not header_value:
        return []
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


def parsed_date(msg: Message) -> Optional[object]:
    """Return the Date header as a tz-aware datetime, or None."""
    raw = msg.get("Date")
    if not raw:
        return None
    try:
        return email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
