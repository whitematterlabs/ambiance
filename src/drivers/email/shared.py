"""Shared on-disk helpers for email drivers.

Per `src/guides/EMAILS.md`, every provider produces the same shape:
    live/communication/email/{account}/
        {YYYY-MM-DD}/{subject-slug}.yaml         # canonical
        {YYYY-MM-DD}/{subject-slug}.prev -> ...  # one-hop walkback
        threads/{thread-slug}/...yaml -> ...     # chronological index

Pure functions, provider-agnostic. Outlook will reuse this module.
"""

from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable, Optional

import yaml

_RE_PREFIX = re.compile(r"^\s*(re|fw|fwd|aw)\s*[:\-]\s*", re.IGNORECASE)
_RE_NONALNUM = re.compile(r"[^a-z0-9]+")


def _strip_subject_prefixes(subject: str) -> str:
    s = subject or ""
    while True:
        new = _RE_PREFIX.sub("", s, count=1)
        if new == s:
            return s
        s = new


def normalize_subject(subject: str) -> str:
    s = _strip_subject_prefixes(subject).lower()
    s = _RE_NONALNUM.sub("-", s).strip("-")
    return s or "no-subject"


def subject_slug(subject: str) -> str:
    """Filesystem-safe slug derived from subject. Cap at 80 chars."""
    s = normalize_subject(subject)
    return s[:80]


def thread_slug(subject: str, references: Optional[Iterable[str]], message_id: str) -> str:
    """Stable per-thread slug. Hash of the root Message-ID gives uniqueness
    across threads that share a normalized subject."""
    refs = list(references or [])
    root = refs[0] if refs else message_id
    h = hashlib.sha1(root.encode("utf-8")).hexdigest()[:8]
    return f"{normalize_subject(subject)}-{h}"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as f:
        f.write(text)
    os.replace(tmp, path)


def write_message_yaml(account_dir: Path, msg: dict) -> Path:
    """Write per-message yaml under {account_dir}/{date}/{subject-slug}.yaml.

    Uses `received_at` for inbound, falls back to `sent_at` for outbound.
    Appends `-{HH-MM}` on same-day slug collision.
    """
    ts = msg.get("received_at") or msg.get("sent_at")
    if not ts:
        raise ValueError("message must have received_at or sent_at")
    dt = ts if isinstance(ts, datetime) else datetime.fromisoformat(ts)
    date_dir = account_dir / dt.date().isoformat()
    slug = subject_slug(msg.get("subject", ""))

    path = date_dir / f"{slug}.yaml"
    if path.exists():
        path = date_dir / f"{slug}-{dt.strftime('%H-%M')}.yaml"
        # Last-resort: append seconds.
        if path.exists():
            path = date_dir / f"{slug}-{dt.strftime('%H-%M-%S')}.yaml"

    body = yaml.safe_dump(msg, sort_keys=False, allow_unicode=True)
    _atomic_write(path, body)
    return path


def link_thread(account_dir: Path, msg_path: Path, t_slug: str, received_at: datetime) -> Path:
    """Create threads/{t_slug}/{YYYY-MM-DD}T{HH-MM}-{subject-slug}.yaml -> msg_path."""
    threads_dir = account_dir / "threads" / t_slug
    threads_dir.mkdir(parents=True, exist_ok=True)
    stem = msg_path.stem
    name = f"{received_at.strftime('%Y-%m-%dT%H-%M')}-{stem}.yaml"
    link = threads_dir / name
    if link.is_symlink() or link.exists():
        return link
    target = os.path.relpath(msg_path, start=threads_dir)
    os.symlink(target, link)
    return link


def link_prev(msg_path: Path, parent_msg_path: Optional[Path]) -> Optional[Path]:
    """Best-effort `.prev` symlink next to msg_path. No-op when parent unknown."""
    if parent_msg_path is None:
        return None
    link = msg_path.with_suffix(".prev")
    if link.is_symlink() or link.exists():
        return link
    target = os.path.relpath(parent_msg_path, start=msg_path.parent)
    os.symlink(target, link)
    return link


def find_message_by_id(account_dir: Path, message_id: str) -> Optional[Path]:
    """Linear scan of date dirs for a yaml containing this Message-ID."""
    if not message_id or not account_dir.exists():
        return None
    needle = f"message_id: {message_id}"
    needle_quoted = f"message_id: '{message_id}'"
    needle_dquoted = f'message_id: "{message_id}"'
    # Iterate newest first — replies usually point to recent parents.
    date_dirs = sorted(
        (p for p in account_dir.iterdir() if p.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.name)),
        reverse=True,
    )
    for d in date_dirs:
        for yml in d.glob("*.yaml"):
            try:
                head = yml.read_text(errors="replace")
            except OSError:
                continue
            if needle in head or needle_quoted in head or needle_dquoted in head:
                return yml
    return None


class _TextExtractor(HTMLParser):
    _SKIP = {"script", "style", "head"}
    _BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BLOCK:
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag in self._BLOCK:
            self._parts.append("\n")

    def handle_data(self, data):
        if self._skip_depth == 0:
            self._parts.append(data)

    def text(self) -> str:
        raw = "".join(self._parts)
        # Collapse runs of blank lines and trim trailing whitespace per line.
        lines = [ln.rstrip() for ln in raw.splitlines()]
        out: list[str] = []
        blank = 0
        for ln in lines:
            if ln.strip():
                out.append(ln)
                blank = 0
            else:
                blank += 1
                if blank <= 1:
                    out.append("")
        return "\n".join(out).strip() + "\n"


def html_to_text(html: str) -> str:
    p = _TextExtractor()
    p.feed(html or "")
    p.close()
    return p.text()
