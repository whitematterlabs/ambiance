#!/usr/bin/env python
"""imessage-backfill — replay iMessage history into communication/messages/.

Queries chat.db for messages on a given date (or range) and replays each one
through kernel.messages.ingest() in chronological order. Day-files for the
target dates are deleted first so re-running is idempotent.

SAFETY: the imessage outbound driver tails day-files for `[HH:MM] me: ...`
lines and ships them via Messages.app. Writing historical `me:` lines via
ingest() would re-send years of texts. So:
  1. Refuses to run while imessage-out is `running`.
  2. After writing day-files, seeds the imessage-out tailer cursors at EOF
     for every touched file so the next driver start treats them as already
     sent. Pass --no-seed to skip (only safe if the driver will never run
     against this filesystem again).

Does NOT touch the imessage_in cursor — backfill is independent of the
live ROWID stream.

Usage:
    imessage-backfill 2026-04-22                  # single day
    imessage-backfill 2026-04-20 2026-04-22       # inclusive range
    imessage-backfill --since 2026-04-20          # from date through today
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import yaml

from drivers.imessage.inbound import (
    CHAT_DB,
    CURSOR_PATH as IN_CURSOR,
    DELTA_SQL,
    MAC_EPOCH,
    _decode_attributed_body,
    _load_cursor as _load_in_cursor,
    _mac_date_to_iso,
    _save_cursor as _save_in_cursor,
)
from kernel import processes as P
from kernel.messages import MESSAGES_DIR, ingest

OUT_DRIVER_SLUG = "imessage-out"
IN_DRIVER_SLUG = "imessage-in"
OUT_CURSORS = P.HOME_DIR / "tmp" / "drivers" / OUT_DRIVER_SLUG / "cursors.yaml"


def _local_day_to_mac_range(start_d: date, end_d: date) -> tuple[int, int]:
    """[start, end) in chat.db nanos-since-2001-UTC for an inclusive local-date range."""
    start_local = datetime.combine(start_d, time.min).astimezone()
    end_local = datetime.combine(end_d + timedelta(days=1), time.min).astimezone()
    start_ns = int((start_local.astimezone(timezone.utc) - MAC_EPOCH).total_seconds() * 1e9)
    end_ns = int((end_local.astimezone(timezone.utc) - MAC_EPOCH).total_seconds() * 1e9)
    return start_ns, end_ns


def _clear_day_files(dates: list[date]) -> int:
    """Delete every messages/{thread}/{date}.md so replay is idempotent."""
    if not MESSAGES_DIR.exists():
        return 0
    fnames = {f"{d.isoformat()}.md" for d in dates}
    removed = 0
    for thread_dir in MESSAGES_DIR.iterdir():
        if not thread_dir.is_dir():
            continue
        for fname in fnames:
            f = thread_dir / fname
            if f.exists():
                f.unlink()
                removed += 1
    return removed


def _daterange(start_d: date, end_d: date) -> list[date]:
    return [start_d + timedelta(days=i) for i in range((end_d - start_d).days + 1)]


def _proc_running(slug: str) -> bool:
    try:
        return P.read_status(slug) == "running"
    except P.ProcessNotFound:
        return False


def _seed_outbound_cursors(touched: set[Path]) -> int:
    """Set the imessage-out cursor at EOF for every touched file so the
    driver treats them as already shipped. Atomic write of cursors.yaml."""
    cursors: dict[str, int] = {}
    if OUT_CURSORS.exists():
        with OUT_CURSORS.open() as f:
            cursors = yaml.safe_load(f) or {}
    seeded = 0
    for path in touched:
        if not path.exists():
            continue
        rel = str(path.relative_to(P.HOME_DIR))
        cursors[rel] = path.stat().st_size
        seeded += 1
    OUT_CURSORS.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT_CURSORS.with_suffix(".yaml.tmp")
    with tmp.open("w") as f:
        yaml.safe_dump(cursors, f, sort_keys=True)
    os.replace(tmp, OUT_CURSORS)
    return seeded


def backfill(start_d: date, end_d: date, seed: bool = True) -> int:
    if not CHAT_DB.exists():
        print(f"chat.db not found at {CHAT_DB}", file=sys.stderr)
        return 1
    if end_d < start_d:
        print(f"end {end_d} is before start {start_d}", file=sys.stderr)
        return 2
    if seed:
        blockers = [s for s in (OUT_DRIVER_SLUG, IN_DRIVER_SLUG) if _proc_running(s)]
        if blockers:
            stop_cmd = " && ".join(f"bin/paictl stop {s}" for s in blockers)
            print(
                f"refusing: {', '.join(blockers)} running. Stop them so backfill "
                f"stays silent (no replays, no nudges):\n  {stop_cmd}",
                file=sys.stderr,
            )
            return 3

    dates = _daterange(start_d, end_d)
    start_ns, end_ns = _local_day_to_mac_range(start_d, end_d)
    label = start_d.isoformat() if start_d == end_d else f"{start_d.isoformat()}..{end_d.isoformat()}"
    print(f"Backfilling {label} ({len(dates)} day{'s' if len(dates) != 1 else ''}) ...")

    conn = sqlite3.connect(str(CHAT_DB))
    conn.execute("PRAGMA query_only = ON")
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            DELTA_SQL.replace("WHERE m.ROWID > ?", "WHERE m.date >= ? AND m.date < ?"),
            (start_ns, end_ns),
        ).fetchall()
    finally:
        conn.close()

    print(f"  {len(rows)} rows in chat.db for that range")
    cleared = _clear_day_files(dates)
    print(f"  cleared {cleared} existing day-files")

    ingested = 0
    skipped = 0
    touched: set[Path] = set()
    max_rowid = 0
    for row in rows:
        max_rowid = max(max_rowid, int(row["rowid"]))
        text = row["text"]
        if text is None:
            text = _decode_attributed_body(row["attributed_body"])
        if text is None or not (row["handle"] or "").strip():
            skipped += 1
            continue
        chat_guid = row["chat_guid"] or ""
        is_group = chat_guid and int(row["participant_count"] or 0) > 1
        received_at = datetime.fromisoformat(_mac_date_to_iso(int(row["mac_date"])))
        sender_override = "me" if bool(row["is_from_me"]) else None
        result = ingest(
            handle=row["handle"],
            text=text,
            chat_guid=chat_guid if is_group else None,
            received_at=received_at,
            source="imessage",
            sender_override=sender_override,
        )
        touched.add(result.day_file)
        ingested += 1

    if seed:
        if touched:
            n = _seed_outbound_cursors(touched)
            print(f"  seeded {n} {OUT_DRIVER_SLUG} cursors at EOF (no replay on next start)")
        if max_rowid:
            in_cur = _load_in_cursor() or 0
            if max_rowid > in_cur:
                _save_in_cursor(max_rowid)
                print(f"  advanced {IN_DRIVER_SLUG} cursor {in_cur} -> {max_rowid} (no nudges for replayed rows)")

    print(f"Done: {ingested} ingested, {skipped} skipped (no body/handle).")
    return 0


def _parse_date(s: str) -> date:
    try:
        return date.fromisoformat(s)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="imessage-backfill",
        description="Replay iMessage history from chat.db into home/communication/messages/.",
    )
    parser.add_argument(
        "dates",
        nargs="*",
        type=_parse_date,
        help="single date YYYY-MM-DD, or START END (inclusive)",
    )
    parser.add_argument(
        "--since",
        type=_parse_date,
        help="backfill from this date through today (inclusive)",
    )
    parser.add_argument(
        "--no-seed",
        action="store_true",
        help=(
            "skip seeding imessage-out cursors and skip the running-driver guard "
            "(only safe if the outbound driver will not run against this filesystem)"
        ),
    )
    args = parser.parse_args(argv)
    seed = not args.no_seed

    if args.since and args.dates:
        print("error: pass either --since or positional date(s), not both", file=sys.stderr)
        return 2
    if args.since:
        return backfill(args.since, date.today(), seed=seed)
    if len(args.dates) == 1:
        return backfill(args.dates[0], args.dates[0], seed=seed)
    if len(args.dates) == 2:
        return backfill(args.dates[0], args.dates[1], seed=seed)
    parser.print_help(sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
