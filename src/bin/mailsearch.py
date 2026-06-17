#!/usr/bin/env python
"""mailsearch — lazy email search via Mail.app's Envelope Index.

Queries the same SQLite index macmail-in watches for new mail, but does
it on demand against the entire history Mail.app has cached. Each hit
gets materialized into the canonical yaml tree under
`var/spool/communication/email/{account}/{date}/...` (idempotent — uses
`shared.write_message_yaml`'s Message-ID dedup), so future greps and
PAI's reply flow "just work" on the result.

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
from typing import Any, Optional

import yaml

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
        description="Lazy email search via Mail.app's Envelope Index. "
                    "Hits get materialized as canonical yamls under "
                    "var/spool/communication/email/.",
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


def run_search(args: argparse.Namespace) -> int:
    # Refresh from Mail.app via AppleScript so newly-added accounts and
    # localized mailbox names are picked up without requiring the kernel
    # to have run recently. Falls back to the persisted file on failure.
    try:
        cfg = asyncio.run(A.refresh())
    except RuntimeError:
        cfg = A.load()
    if cfg.is_empty():
        print(
            "mailsearch: no Mail.app accounts discovered (is Mail.app running "
            "and is automation permission granted?)",
            file=sys.stderr,
        )
        return 2

    sql, params = build_query(args, cfg)

    if not IN.ENVELOPE_INDEX.exists():
        print(f"mailsearch: Envelope Index not found at {IN.ENVELOPE_INDEX}",
              file=sys.stderr)
        return 2

    try:
        conn = IN._connect()
    except sqlite3.OperationalError as e:
        print(f"mailsearch: cannot open Envelope Index: {e}", file=sys.stderr)
        return 2

    try:
        rows = conn.execute(sql, params).fetchall()
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
            "path": result["path"],
            "date": ts.isoformat(timespec="seconds"),
            "account": result["account"],
            "from": result["from"],
            "subject": result["subject"],
        })

    if not results:
        print("[]", end="")
        return 0
    yaml.safe_dump(results, sys.stdout, sort_keys=False, allow_unicode=True)
    return 0


def main() -> None:
    args = parse_args(sys.argv[1:])
    sys.exit(run_search(args))


if __name__ == "__main__":
    main()
