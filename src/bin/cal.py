"""cal — print Apple Calendar events from the calendar driver's on-disk state.

Reads flat YAML event files from /calendar/YYYY-MM-DD/<uid>.yaml (written
by the calendar-in driver) and prints them formatted for the terminal.

Usage:
    cal --today              print today's events
    cal --date 2026-05-08    print events for a specific date
    cal --upcoming [N]       print events for the next N days (default 7)
    cal --help               show this help
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

# Calendar data root — the calendar-in driver writes here.
# Follows paths.PAI_ROOT so it works inside and outside the PAI env.
PAI_ROOT = Path(os.environ.get("PAI_ROOT", str(Path.home() / ".pai")))
CALENDAR_DIR = PAI_ROOT / "calendar"


def _load_events_for_date(d: date) -> list[dict]:
    """Load all event YAML files for a given date, sorted by start time."""
    date_dir = CALENDAR_DIR / d.isoformat()
    if not date_dir.exists():
        return []
    events: list[dict] = []
    for yf in sorted(date_dir.glob("*.yaml")):
        try:
            with yf.open() as f:
                data = yaml.safe_load(f) or {}
        except yaml.YAMLError:
            continue
        if data:
            events.append(data)
    # Sort by start_date
    events.sort(key=lambda e: e.get("start_date", ""))
    return events


def _format_time(event: dict) -> str:
    """Format the time portion of an event for display."""
    if event.get("all_day"):
        return "all day"

    start_str = event.get("start_date", "")
    end_str = event.get("end_date", "")

    try:
        start_dt = datetime.fromisoformat(start_str)
    except (ValueError, TypeError):
        return start_str

    try:
        end_dt = datetime.fromisoformat(end_str)
    except (ValueError, TypeError):
        return start_dt.strftime("%-I:%M %p")

    # Format as "4:00 PM – 5:00 PM" or "4:00 – 5:00 PM" if same AM/PM
    start_fmt = start_dt.strftime("%-I:%M")
    end_fmt = end_dt.strftime("%-I:%M")
    start_ampm = start_dt.strftime("%p")
    end_ampm = end_dt.strftime("%p")

    if start_ampm == end_ampm:
        return f"{start_fmt} – {end_fmt} {end_ampm}"
    else:
        return f"{start_fmt} {start_ampm} – {end_fmt} {end_ampm}"


def _print_events(events: list[dict], heading: str) -> None:
    """Print a formatted list of events."""
    if not events:
        print(f"{heading}: no events")
        return

    print(heading)
    print("-" * 60)
    for evt in events:
        time_str = _format_time(evt)
        title = evt.get("title", "(no title)")
        cal_name = evt.get("calendar_name", "")
        location = evt.get("location", "")

        # Build the display line
        parts = [f"  {time_str}", title]
        if cal_name:
            parts.append(f"[{cal_name}]")
        print("  ".join(parts))

        if location:
            print(f"    📍 {location}")

        notes = evt.get("notes", "")
        if notes:
            # Indent multi-line notes
            for note_line in notes.splitlines():
                print(f"    📝 {note_line}")

        url = evt.get("url", "")
        if url:
            print(f"    🔗 {url}")

        print()  # blank line between events
    print("-" * 60)


def cmd_today() -> None:
    """Print today's events."""
    today = date.today()
    events = _load_events_for_date(today)
    _print_events(events, f"Today — {today.strftime('%A, %B %-d, %Y')}")


def cmd_date(date_str: str) -> None:
    """Print events for a specific date."""
    try:
        d = date.fromisoformat(date_str)
    except ValueError:
        print(f"cal: invalid date '{date_str}' — use YYYY-MM-DD format", file=sys.stderr)
        sys.exit(1)
    events = _load_events_for_date(d)
    _print_events(events, d.strftime("%A, %B %-d, %Y"))


def cmd_upcoming(n_days: int) -> None:
    """Print events for the next N days."""
    today = date.today()
    all_events: list[tuple[date, list[dict]]] = []

    for i in range(n_days):
        d = today + timedelta(days=i)
        events = _load_events_for_date(d)
        if events:
            all_events.append((d, events))

    if not all_events:
        print(f"No events in the next {n_days} days.")
        return

    for d, events in all_events:
        heading = d.strftime("%A, %B %-d, %Y")
        if d == today:
            heading += " (today)"
        elif d == today + timedelta(days=1):
            heading += " (tomorrow)"
        _print_events(events, heading)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="cal",
        description="Print Apple Calendar events from local cache.",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--today", action="store_true", help="Print today's events")
    group.add_argument("--date", type=str, metavar="YYYY-MM-DD", help="Print events for a specific date")
    group.add_argument("--upcoming", type=int, nargs="?", const=7, metavar="N",
                       help="Print events for the next N days (default 7)")

    args = parser.parse_args()

    # Default: --today
    if not args.today and not args.date and args.upcoming is None:
        args.today = True

    if args.today:
        cmd_today()
    elif args.date:
        cmd_date(args.date)
    elif args.upcoming is not None:
        cmd_upcoming(args.upcoming)


if __name__ == "__main__":
    main()
