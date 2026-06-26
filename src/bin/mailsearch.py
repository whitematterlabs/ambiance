#!/usr/bin/env python
"""mailsearch — email search over two sources, deduped.

Searches BOTH:
  1. The on-disk canonical yaml archive under
     `~/communication/email/{account}/...` — everything the driver has
     already ingested, including mail Mail.app has since deleted or
     aged out of its index. Works even when Mail.app is closed or the
     Envelope Index is gone.
  2. Mail.app's SQLite Envelope Index — the entire history Mail.app has
     cached, including mail older than the driver's ingest window. Each
     SQLite hit gets materialized into the canonical yaml tree
     (idempotent — uses `shared.write_message_yaml`'s Message-ID dedup),
     so future greps and PAI's reply flow "just work" on the result.

Results from the two sources are merged and deduped by Message-ID (path
fallback), newest first. A message present in both sources appears once.

Result `path`s are emitted in the home view (`communication/email/...`),
matching what the email SKILL teaches and what the calling PAI can grep
or read directly — not the FHS `var/spool/...` spelling.

`--from` / `--to` match the address substring OR the contact's display
name, so both `--from cjnewton@mit.edu` and `--from "Curt Newton"` hit.

Usage:
    mailsearch --from bob@example.com --limit 10
    mailsearch --subject "Q3 budget" --since 2025-01-01
    mailsearch --to me@example.com --account arda@icloud.com --unread
    mailsearch --flagged --since 2024-06-01

At least one of --from, --to, --subject, --since is required so we
don't accidentally materialize the whole index.
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

from boot import paths
from drivers.email import shared
from drivers.email.macmail import accounts as A
from drivers.email.macmail import inbound as IN


DEFAULT_LIMIT = 20
MAX_LIMIT = 200


def _split_terms(value: Optional[str]) -> list[str]:
    """Split a flag value on `|` so callers can pass alternations like
    `--subject "shipped|tracking"`. Empty pieces are dropped. Returns
    `[]` when value is None or only separators."""
    if value is None:
        return []
    return [t for t in (p.strip() for p in value.split("|")) if t]


def build_query(args: argparse.Namespace, cfg: A.AccountsConfig) -> tuple[str, list[Any]]:
    """Return (sql, params) honoring whichever flags the user supplied.

    `--subject`, `--from`, `--to`, and `--account` accept `|`-separated
    alternations: each term becomes its own LIKE clause OR-joined together.
    Absent filters are simply omitted from the WHERE clause.

    The mailbox-URL filter is built from `cfg.url_like_patterns()` so it
    matches whatever mailbox names Mail.app reports per account, in any
    locale (e.g. Turkish `Gelen Kutusu`).
    """
    role_patterns = cfg.url_like_patterns()
    if args.inbox_only:
        role_patterns = [(p, r) for p, r in role_patterns if r == "inbound"]
    mb_patterns = [pat for pat, _role in role_patterns]

    where: list[str] = ["m.deleted = 0"]
    params: list[Any] = []

    if not mb_patterns:
        where.append("1 = 0")
    else:
        where.append("(" + " OR ".join(["mb.url LIKE ?"] * len(mb_patterns)) + ")")
        params.extend(mb_patterns)

    def add_or_likes(column: str, terms: list[str]) -> None:
        add_or_likes_cols([column], terms)

    def add_or_likes_cols(columns: list[str], terms: list[str]) -> None:
        """Match each term against any of `columns` (substring LIKE), with
        the terms OR-joined. Used so an address filter matches both the
        raw address and its display-name `comment` — `--from newton` finds
        `cjnewton@mit.edu`, and `--from "Curt Newton"` finds it too."""
        if not terms:
            return
        per_term = "(" + " OR ".join(f"{c} LIKE ?" for c in columns) + ")"
        where.append("(" + " OR ".join([per_term] * len(terms)) + ")")
        for t in terms:
            params.extend(f"%{t}%" for _ in columns)

    add_or_likes_cols(["sender.address", "sender.comment"],
                      _split_terms(args.from_addr))
    add_or_likes("s.subject", _split_terms(args.subject))

    to_terms = _split_terms(args.to_addr)
    if to_terms:
        ors = " OR ".join(
            ["(ra.address LIKE ? OR ra.comment LIKE ?)"] * len(to_terms)
        )
        where.append(
            "EXISTS (SELECT 1 FROM recipients r "
            "JOIN addresses ra ON ra.ROWID = r.address "
            f"WHERE r.message = m.ROWID AND ({ors}))"
        )
        for t in to_terms:
            params.append(f"%{t}%")
            params.append(f"%{t}%")

    add_or_likes("mb.url", _split_terms(args.account))

    if args.since_ts is not None:
        where.append("m.date_received >= ?")
        params.append(args.since_ts)
    if args.until_ts is not None:
        where.append("m.date_received <= ?")
        params.append(args.until_ts)
    if args.unread:
        where.append("m.read = 0")
    if args.flagged:
        where.append("m.flagged = 1")

    sql = f"""
SELECT
    m.ROWID AS rowid,
    m.date_received AS date_received,
    m.conversation_id AS conversation_id,
    mb.url AS url
FROM messages m
JOIN mailboxes mb ON mb.ROWID = m.mailbox
LEFT JOIN subjects s ON s.ROWID = m.subject
LEFT JOIN addresses sender ON sender.ROWID = m.sender
WHERE {" AND ".join(where)}
ORDER BY m.date_received DESC
LIMIT ?
"""
    params.append(args.limit)
    return sql, params


def parse_date(s: Optional[str]) -> Optional[int]:
    """YYYY-MM-DD → Unix epoch seconds (local-tz midnight)."""
    if s is None:
        return None
    try:
        d = date.fromisoformat(s)
    except ValueError as e:
        raise SystemExit(f"mailsearch: bad date '{s}': {e}")
    dt = datetime.combine(d, time.min).astimezone(timezone.utc)
    return int(dt.timestamp())


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="mailsearch",
        description="Search the on-disk email archive AND Mail.app's "
                    "Envelope Index, merged and deduped by Message-ID. "
                    "Index hits get materialized as canonical yamls under "
                    "~/communication/email/.",
    )
    p.add_argument("--from", dest="from_addr",
                   help="sender address OR display name contains (substring; "
                        "`|` = OR, e.g. 'ups|fedex' or 'Curt Newton')")
    p.add_argument("--to", dest="to_addr",
                   help="any recipient address OR display name contains, "
                        "To/Cc/Bcc (substring; `|` = OR)")
    p.add_argument("--subject",
                   help="subject substring, ASCII case-insensitive; "
                        "`|` = OR (e.g. 'shipped|tracking|delivered'). "
                        "Not a regex — only `|` is special.")
    p.add_argument("--account",
                   help="restrict to mailbox URLs containing this substring; "
                        "`|` = OR")
    p.add_argument("--since", help="YYYY-MM-DD lower bound on date_received")
    p.add_argument("--until", help="YYYY-MM-DD upper bound on date_received")
    p.add_argument("--unread", action="store_true", help="only unread messages")
    p.add_argument("--flagged", action="store_true", help="only flagged messages")
    p.add_argument("--inbox-only", action="store_true",
                   help="exclude Sent folders (default includes Sent)")
    p.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                   help=f"max hits (default {DEFAULT_LIMIT}, max {MAX_LIMIT})")
    args = p.parse_args(argv)

    if not (args.from_addr or args.to_addr or args.subject or args.since):
        p.error(
            "at least one of --from, --to, --subject, --since is required "
            "(otherwise this would materialize the entire index)"
        )

    if args.limit <= 0:
        p.error("--limit must be > 0")
    if args.limit > MAX_LIMIT:
        p.error(f"--limit must be <= {MAX_LIMIT}")

    args.since_ts = parse_date(args.since)
    args.until_ts = parse_date(args.until)
    return args


# ---- on-disk archive search ----------------------------------------------

_EPOCH = datetime.fromtimestamp(0, tz=timezone.utc)


def _parse_dt(s: Optional[str]) -> datetime:
    """ISO string → aware datetime; naive strings assume local tz. Falls
    back to the Unix epoch so a malformed timestamp sorts oldest rather
    than crashing the merge."""
    if not s:
        return _EPOCH
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return _EPOCH
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt


def _any_term_in(terms: list[str], *haystacks: Optional[str]) -> bool:
    """True if no terms (filter absent) or any term is a case-insensitive
    substring of the concatenated haystacks — mirrors the SQLite side's
    OR-of-LIKEs semantics for `|`-alternations."""
    if not terms:
        return True
    blob = " ".join(h for h in haystacks if h).lower()
    return any(t.lower() in blob for t in terms)


def search_disk(args: argparse.Namespace) -> list[dict]:
    """Scan the canonical yaml archive under var/spool/communication/email,
    applying the same filters as the SQLite query.

    Independent of Mail.app: reads only on-disk yamls, so it surfaces mail
    that Mail.app has deleted or never kept in its index. `--unread` /
    `--flagged` are live-state predicates with no on-disk equivalent, so a
    disk scan is skipped when either is set (the caller notes this)."""
    root = paths.var_spool_email()
    if not root.exists():
        return []

    from_terms = _split_terms(args.from_addr)
    to_terms = _split_terms(args.to_addr)
    subject_terms = _split_terms(args.subject)
    account_terms = _split_terms(args.account)

    out: list[dict] = []
    for acc_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        # `drafts/` holds unsent drafts, not received/sent mail; the SQLite
        # side never returns them, so skip for parity.
        if acc_dir.name == "drafts":
            continue
        if not _any_term_in(account_terms, acc_dir.name):
            continue
        for yml in acc_dir.rglob("*.yaml"):
            # threads/ entries are symlinks back to the canonical files;
            # meta.yaml is account metadata, not a message.
            parts = yml.relative_to(acc_dir).parts
            if "threads" in parts or yml.name == "meta.yaml":
                continue
            try:
                msg = yaml.safe_load(yml.read_text(errors="replace")) or {}
            except (OSError, yaml.YAMLError):
                continue
            if not isinstance(msg, dict):
                continue

            if args.inbox_only and msg.get("direction") == "outbound":
                continue
            if not _any_term_in(from_terms, msg.get("from"), msg.get("from_name")):
                continue
            if not _any_term_in(subject_terms, msg.get("subject")):
                continue
            if to_terms:
                recips = [
                    a for k in ("to", "cc", "bcc")
                    for a in (msg.get(k) or [])
                    if isinstance(a, str)
                ]
                if not _any_term_in(to_terms, *recips):
                    continue

            ts = _parse_dt(msg.get("received_at") or msg.get("sent_at"))
            if args.since_ts is not None and ts.timestamp() < args.since_ts:
                continue
            if args.until_ts is not None and ts.timestamp() > args.until_ts:
                continue

            out.append({
                "path": shared.home_view_path(str(yml.relative_to(paths.PAI_ROOT))),
                "date": ts.isoformat(timespec="seconds"),
                "account": acc_dir.name,
                "from": msg.get("from", ""),
                "subject": msg.get("subject", ""),
                "_mid": (msg.get("message_id") or "").strip(),
                "_sort": ts,
            })
    return out


def search_sqlite(args: argparse.Namespace, cfg: A.AccountsConfig) -> list[dict]:
    """Query Mail.app's Envelope Index and materialize each hit. Returns []
    (and warns) on any access failure rather than aborting the whole search —
    the on-disk scan still runs."""
    if cfg.is_empty():
        print(
            "[mailsearch] no Mail.app accounts discovered; searching on-disk "
            "archive only (is Mail.app running and automation permitted?)",
            file=sys.stderr,
        )
        return []
    if not IN.ENVELOPE_INDEX.exists():
        print(
            f"[mailsearch] Envelope Index not found at {IN.ENVELOPE_INDEX}; "
            "searching on-disk archive only",
            file=sys.stderr,
        )
        return []
    try:
        conn = IN._connect()
    except sqlite3.OperationalError as e:
        print(f"[mailsearch] cannot open Envelope Index: {e}; "
              "searching on-disk archive only", file=sys.stderr)
        return []

    sql, params = build_query(args, cfg)
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as e:
        print(f"[mailsearch] index query failed: {e}; "
              "searching on-disk archive only", file=sys.stderr)
        return []
    finally:
        conn.close()

    results: list[dict] = []
    for row in rows:
        try:
            result = IN.ingest_row(row, cfg)
        except Exception as e:
            print(f"[mailsearch] error rowid={row['rowid']}: {e!r}", file=sys.stderr)
            continue
        if result is None:
            print(f"[mailsearch] skipped rowid={row['rowid']}: emlx not on disk",
                  file=sys.stderr)
            continue
        if result.get("_skip"):
            print(f"[mailsearch] skipped rowid={row['rowid']}: parse failure",
                  file=sys.stderr)
            continue
        ts = IN._mac_date_to_dt(int(row["date_received"] or 0))
        results.append({
            "path": shared.home_view_path(result["path"]),
            "date": ts.isoformat(timespec="seconds"),
            "account": result["account"],
            "from": result["from"],
            "subject": result["subject"],
            "_mid": (result.get("_message_id") or "").strip(),
            "_sort": ts,
        })
    return results


def merge_dedupe(sqlite_hits: list[dict], disk_hits: list[dict], limit: int) -> list[dict]:
    """Combine both sources, dedupe by Message-ID (path fallback), sort
    newest-first, and cap at `limit`. SQLite hits are considered first so a
    message present in both keeps the freshly-materialized live copy."""
    merged: list[dict] = []
    seen: set[str] = set()
    for hit in sqlite_hits + disk_hits:
        key = hit.get("_mid") or hit["path"]
        if key in seen:
            continue
        seen.add(key)
        merged.append(hit)
    merged.sort(key=lambda h: h["_sort"], reverse=True)
    return merged[:limit]


def run_search(args: argparse.Namespace) -> int:
    # Refresh from Mail.app via AppleScript so newly-added accounts and
    # localized mailbox names are picked up without requiring the kernel
    # to have run recently. Falls back to the persisted file on failure.
    try:
        cfg = asyncio.run(A.refresh())
    except RuntimeError:
        cfg = A.load()

    sqlite_hits = search_sqlite(args, cfg)

    if args.unread or args.flagged:
        # read/flag are live Mail.app state with no on-disk equivalent;
        # honoring them means the SQLite source only.
        print("[mailsearch] --unread/--flagged are live-state filters; "
              "skipping the on-disk archive scan", file=sys.stderr)
        disk_hits: list[dict] = []
    else:
        disk_hits = search_disk(args)

    if not sqlite_hits and not disk_hits:
        print("[]", end="")
        return 0

    results = merge_dedupe(sqlite_hits, disk_hits, args.limit)
    for r in results:
        r.pop("_mid", None)
        r.pop("_sort", None)
    yaml.safe_dump(results, sys.stdout, sort_keys=False, allow_unicode=True)
    return 0


def main() -> None:
    args = parse_args(sys.argv[1:])
    sys.exit(run_search(args))


if __name__ == "__main__":
    main()
