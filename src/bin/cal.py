"""cal — print today's macOS Calendar events via EventKit.

Usage:
    cal                        print today's events
    cal --date YYYY-MM-DD      print events for a specific day
"""

from __future__ import annotations

import argparse
import sys
import threading
from datetime import date, datetime, time, timedelta

from EventKit import EKEventStore, EKEntityTypeEvent
from Foundation import NSDate


def _request_access(store: EKEventStore) -> bool:
    done = threading.Event()
    result = {"granted": False}

    def handler(granted, err):
        result["granted"] = bool(granted)
        done.set()

    if hasattr(store, "requestFullAccessToEventsWithCompletion_"):
        store.requestFullAccessToEventsWithCompletion_(handler)
    else:
        store.requestAccessToEntityType_completion_(EKEntityTypeEvent, handler)

    done.wait(timeout=30)
    return result["granted"]


def _nsdate(dt: datetime) -> NSDate:
    return NSDate.dateWithTimeIntervalSince1970_(dt.timestamp())


def _fmt_time(dt: datetime) -> str:
    return dt.strftime("%-I:%M %p")


def _print_for_date(d: date) -> None:
    store = EKEventStore.alloc().init()
    if not _request_access(store):
        print("cal: calendar access denied. Grant access in System Settings > Privacy & Security > Calendars.", file=sys.stderr)
        sys.exit(1)

    start = datetime.combine(d, time.min).astimezone()
    end = datetime.combine(d, time.max).astimezone()

    calendars = store.calendarsForEntityType_(EKEntityTypeEvent)
    predicate = store.predicateForEventsWithStartDate_endDate_calendars_(
        _nsdate(start), _nsdate(end), calendars
    )
    events = store.eventsMatchingPredicate_(predicate) or []

    heading = d.strftime("%A, %B %-d, %Y")
    if not events:
        print(f"{heading}: no events")
        return

    print(heading)
    print("-" * 60)

    def sort_key(e):
        sd = e.startDate()
        return sd.timeIntervalSince1970() if sd else 0

    for evt in sorted(events, key=sort_key):
        title = evt.title() or "(no title)"
        cal = evt.calendar()
        cal_name = cal.title() if cal else ""

        if evt.isAllDay():
            time_str = "all day"
        else:
            sd = evt.startDate()
            ed = evt.endDate()
            s_dt = datetime.fromtimestamp(sd.timeIntervalSince1970()) if sd else None
            e_dt = datetime.fromtimestamp(ed.timeIntervalSince1970()) if ed else None
            if s_dt and e_dt:
                time_str = f"{_fmt_time(s_dt)} – {_fmt_time(e_dt)}"
            elif s_dt:
                time_str = _fmt_time(s_dt)
            else:
                time_str = "?"

        line = f"  {time_str}  {title}"
        if cal_name:
            line += f"  [{cal_name}]"
        print(line)

    print("-" * 60)


def main() -> int:
    parser = argparse.ArgumentParser(prog="cal", description="Print macOS Calendar events for a day.")
    parser.add_argument("--date", metavar="YYYY-MM-DD", help="Date to query (default: today)")
    args = parser.parse_args()

    if args.date:
        try:
            d = date.fromisoformat(args.date)
        except ValueError:
            print(f"cal: invalid date '{args.date}' — use YYYY-MM-DD", file=sys.stderr)
            return 1
    else:
        d = date.today()

    _print_for_date(d)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
