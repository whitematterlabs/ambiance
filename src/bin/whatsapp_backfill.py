#!/Users/arda/.pai/usr/bin/env python
"""whatsapp-backfill — replay WhatsApp history into communication/messages/.

Queries ChatStorage.sqlite for messages on a given date (or range) and replays
each one through drivers.messages.ingest() in chronological order. Day-files
for the target dates are deleted first so re-running is idempotent.

SAFETY: the whatsapp outbound driver tails day-files for bare lines and ships
them via the Baileys bridge. Writing historical lines via ingest() could
re-send years of texts. So:
  1. Refuses to run while whatsapp-out is `running`.
  2. After writing day-files, seeds the whatsapp-out tailer cursors at EOF
     for every touched file so the next driver start treats them as already
     sent. Pass --no-seed to skip (only safe if the driver will never run
     against this filesystem again).

Does NOT touch the whatsapp-in cursor — backfill is independent of the
live Baileys bridge stream.

Usage:
    whatsapp-backfill 2026-04-22                  # single day
    whatsapp-backfill 2026-04-20 2026-04-22       # inclusive range
    whatsapp-backfill --since 2026-04-20           # from date through today
    whatsapp-backfill --days 7                     # last 7 days
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import yaml

from boot import processes as P
from boot.paths import PAI_ROOT
from drivers.messages import MESSAGES_DIR, ingest

# ── constants ────────────────────────────────────────────────────────────
CHATSTORAGE_DB = (
    Path.home()
    / "Library"
    / "Group Containers"
    / "group.net.whatsapp.WhatsApp.shared"
    / "ChatStorage.sqlite"
)
MAC_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
OUT_DRIVER_SLUG = "whatsapp-out"
OUT_CURSORS = P.HOME_DIR / "tmp" / "drivers" / OUT_DRIVER_SLUG / "cursors.yaml"

# ── SQL ───────────────────────────────────────────────────────────────────
QUERY = """
SELECT
    m.Z_PK,
    m.ZTEXT,
    m.ZFROMJID,
    m.ZTOJID,
    m.ZISFROMME,
    m.ZMESSAGEDATE,
    s.ZCONTACTJID,
    s.ZPARTNERNAME
FROM ZWAMESSAGE m
JOIN ZWACHATSESSION s ON m.ZCHATSESSION = s.Z_PK
WHERE m.ZMESSAGETYPE = 0
  AND s.ZSESSIONTYPE = 0
  AND m.ZMESSAGEDATE >= ? AND m.ZMESSAGEDATE < ?
ORDER BY m.ZMESSAGEDATE ASC
"""

# ── helpers ───────────────────────────────────────────────────────────────

def _mac_timestamp_to_datetime(ts: float) -> datetime:
    """Convert CoreData timestamp (seconds since 2001-01-01 UTC) to datetime."""
    return MAC_EPOCH + timedelta(seconds=ts)


def _resolve_handle(zcontactjid: str | None, zpartnername: str | None) -> str | None:
    """Resolve a ZCONTACTJID + ZPARTNERNAME to a usable handle.

    - @s.whatsapp.net → strip domain, use the phone number
    - @lid → try to extract a phone from partner name, else use LID
    - other / missing → return None
    """
    if not zcontactjid:
        return None

    jid = zcontactjid.strip()

    # Standard phone-based JID: 15551234567@s.whatsapp.net
    if jid.endswith("@s.whatsapp.net"):
        phone = jid[: -len("@s.whatsapp.net")]
        if phone and phone != "0":
            return phone
        return None

    # LID-based JID: 191508397449230@lid
    if jid.endswith("@lid"):
        lid = jid[: -len("@lid")]
        # Try to extract phone from partner name (e.g. "+1 (408) 757-7469")
        if zpartnername:
            digits = re.sub(r"\D", "", zpartnername)
            if len(digits) >= 10:
                return digits
        # Fall back to LID — it'll create a LID-named thread that can
        # be resolved later via resolve-contact.
        return f"wa:{lid}"

    # Unknown format
    return None


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
    """Set the whatsapp-out cursor at EOF for every touched file so the
    driver treats them as already shipped. Atomic write of cursors.yaml.

    Cursor keys are relative to PAI_ROOT, matching the tailer's _rel().
    """
    cursors: dict[str, int] = {}
    if OUT_CURSORS.exists():
        with OUT_CURSORS.open() as f:
            cursors = yaml.safe_load(f) or {}
    seeded = 0
    pai_root = PAI_ROOT.resolve()
    for path in touched:
        if not path.exists():
            continue
        rel = str(path.resolve().relative_to(pai_root))
        cursors[rel] = path.stat().st_size
        seeded += 1
    OUT_CURSORS.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT_CURSORS.with_suffix(".yaml.tmp")
    with tmp.open("w") as f:
        yaml.safe_dump(cursors, f, sort_keys=True)
    os.replace(tmp, OUT_CURSORS)
    return seeded


# ── core ──────────────────────────────────────────────────────────────────

def backfill(start_d: date, end_d: date, seed: bool = True) -> int:
    if not CHATSTORAGE_DB.exists():
        print(f"ChatStorage.sqlite not found at {CHATSTORAGE_DB}", file=sys.stderr)
        return 1
    if end_d < start_d:
        print(f"end {end_d} is before start {start_d}", file=sys.stderr)
        return 2

    if seed:
        if _proc_running(OUT_DRIVER_SLUG):
            print(
                f"refusing: {OUT_DRIVER_SLUG} running. Stop it so backfill "
                f"stays silent (no replays):\n  bin/paicron stop {OUT_DRIVER_SLUG}",
                file=sys.stderr,
            )
            return 3

    # Convert local dates to CoreData timestamp range [start, end)
    start_local = datetime.combine(start_d, time.min).astimezone()
    end_local = datetime.combine(end_d + timedelta(days=1), time.min).astimezone()
    start_ts = (start_local.astimezone(timezone.utc) - MAC_EPOCH).total_seconds()
    end_ts = (end_local.astimezone(timezone.utc) - MAC_EPOCH).total_seconds()

    dates = _daterange(start_d, end_d)
    label = start_d.isoformat() if start_d == end_d else f"{start_d.isoformat()}..{end_d.isoformat()}"
    print(f"Backfilling {label} ({len(dates)} day{'s' if len(dates) != 1 else ''}) ...")

    conn = sqlite3.connect(str(CHATSTORAGE_DB))
    conn.execute("PRAGMA query_only = ON")
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(QUERY, (start_ts, end_ts)).fetchall()
    finally:
        conn.close()

    print(f"  {len(rows)} rows in ChatStorage.sqlite for that range")
    cleared = _clear_day_files(dates)
    print(f"  cleared {cleared} existing day-files")

    ingested = 0
    skipped = 0
    touched: set[Path] = set()
    max_pk = 0

    for row in rows:
        max_pk = max(max_pk, int(row["Z_PK"] or 0))

        text = (row["ZTEXT"] or "").strip()
        if not text:
            skipped += 1
            continue

        zcontactjid = row["ZCONTACTJID"]
        zpartnername = row["ZPARTNERNAME"]
        handle = _resolve_handle(zcontactjid, zpartnername)
        if not handle:
            skipped += 1
            continue

        ts = row["ZMESSAGEDATE"]
        if ts is None:
            skipped += 1
            continue
        received_at = _mac_timestamp_to_datetime(float(ts))

        is_from_me = bool(row["ZISFROMME"])
        sender_override = "me" if is_from_me else None

        result = ingest(
            handle=handle,
            text=text,
            display_name=zpartnername,
            received_at=received_at,
            source="whatsapp",
            sender_override=sender_override,
        )
        touched.add(result.day_file)
        ingested += 1

    if seed and touched:
        n = _seed_outbound_cursors(touched)
        print(f"  seeded {n} {OUT_DRIVER_SLUG} cursors at EOF (no replay on next start)")

    print(f"Done: {ingested} ingested, {skipped} skipped (no text/handle).")
    return 0


# ── CLI ───────────────────────────────────────────────────────────────────

def _parse_date(s: str) -> date:
    try:
        return date.fromisoformat(s)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="whatsapp-backfill",
        description="Replay WhatsApp history from ChatStorage.sqlite into communication/messages/.",
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
        "--days",
        type=int,
        help="backfill the last N days (through today, inclusive)",
    )
    parser.add_argument(
        "--no-seed",
        action="store_true",
        help=(
            "skip seeding whatsapp-out cursors and skip the running-driver guard "
            "(only safe if the outbound driver will not run against this filesystem)"
        ),
    )
    args = parser.parse_args(argv)
    seed = not args.no_seed

    # Count how many date-specifying modes are used
    modes = sum([bool(args.since), bool(args.days), bool(args.dates)])
    if modes > 1:
        print("error: pass only one of --since, --days, or positional date(s)", file=sys.stderr)
        return 2
    if modes == 0:
        parser.print_help(sys.stderr)
        return 2

    if args.since:
        return backfill(args.since, date.today(), seed=seed)
    if args.days:
        end = date.today()
        start = end - timedelta(days=args.days - 1)
        return backfill(start, end, seed=seed)
    if len(args.dates) == 1:
        return backfill(args.dates[0], args.dates[0], seed=seed)
    if len(args.dates) == 2:
        return backfill(args.dates[0], args.dates[1], seed=seed)
    parser.print_help(sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
