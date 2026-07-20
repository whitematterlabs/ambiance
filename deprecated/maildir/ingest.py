"""Translate a Maildir file into a canonical email YAML.

Pure-ish: takes a path to an RFC 5322 file and an account address, parses
it, writes the YAML under `var/spool/communication/email/<account>/`, and
returns the event-payload dict the inbound driver will hand to the kernel.

Idempotent on Message-ID: if the canonical store already has a yaml with
this Message-ID, no second yaml is written. Returns the existing path.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

from boot import paths

from .. import parse, shared
from . import config as Cfg


def _ensure_meta(account_dir: Path, account: "Cfg.Account") -> None:
    meta = account_dir / "meta.yaml"
    if meta.exists():
        return
    account_dir.mkdir(parents=True, exist_ok=True)
    body = (
        f"account: {account.address}\n"
        f"provider: {account.provider}\n"
        f"created: {datetime.now().date().isoformat()}\n"
    )
    tmp = meta.with_suffix(".yaml.tmp")
    tmp.write_text(body)
    tmp.replace(meta)


def _build_msg_dict(msg, direction: str, ts: datetime) -> dict:
    message_id = (msg.get("Message-ID") or msg.get("Message-Id") or "").strip()
    in_reply_to = (msg.get("In-Reply-To") or "").strip() or None
    references = parse.header_id_list(msg.get("References"))
    from_addrs = parse.extract_addresses(msg.get("From"))
    to_addrs = parse.extract_addresses(msg.get("To"))
    cc_addrs = parse.extract_addresses(msg.get("Cc"))
    bcc_addrs = parse.extract_addresses(msg.get("Bcc"))
    subject = str(msg.get("Subject") or "")
    content = parse.extract_text(msg)

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
    }
    if direction == "outbound":
        out["sent_at"] = ts.isoformat(timespec="seconds")
    else:
        out["received_at"] = ts.isoformat(timespec="seconds")
    return out


def ingest_file(path: Path, account: "Cfg.Account", direction: str = "inbound") -> Optional[dict]:
    """Parse a Maildir file and persist the canonical YAML.

    Returns an event payload (account/thread_slug/subject/from/direction/path),
    or None if parsing failed.
    """
    try:
        data = path.read_bytes()
    except OSError as e:
        print(f"[maildir] cannot read {path}: {e}", flush=True)
        return None
    try:
        msg = parse.parse_bytes(data)
    except Exception as e:
        print(f"[maildir] parse failed {path}: {e}", flush=True)
        return None

    account_dir = paths.var_spool_email() / account.address
    _ensure_meta(account_dir, account)

    message_id = (msg.get("Message-ID") or msg.get("Message-Id") or "").strip()
    existing = shared.find_message_by_id(account_dir, message_id) if message_id else None
    if existing:
        return None  # idempotent: already in canonical store

    ts = parse.parsed_date(msg) or datetime.now().astimezone()
    msg_dict = _build_msg_dict(msg, direction, ts)
    msg_path = shared.write_message_yaml(account_dir, msg_dict)
    shared.link_thread(account_dir, msg_path, msg_dict["thread_slug"], ts)

    parent_id = msg_dict["in_reply_to"] or (
        msg_dict["references"][-1] if msg_dict["references"] else None
    )
    if parent_id:
        parent_path = shared.find_message_by_id(account_dir, parent_id)
        if parent_path:
            shared.link_prev(msg_path, parent_path)

    return {
        "account": account.address,
        "thread_slug": msg_dict["thread_slug"],
        "subject": msg_dict["subject"],
        "from": msg_dict["from"],
        "direction": direction,
        "path": str(msg_path.relative_to(paths.PAI_ROOT)),
    }
