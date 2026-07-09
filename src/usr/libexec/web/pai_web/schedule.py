"""Schedule semantics for owner-editable scheduled tasks — the single source
of truth for the four presets the console exposes.

A scheduled task's `schedule:` field is either a cron expression (recurring) or
an ISO datetime (one-shot), exactly as `paicron`/the kernel already understand
(`boot.timers.parse_schedule`). The console only ever offers four shapes, so all
the cron-string logic lives here — the frontend handles structured fields plus a
display label and never parses cron in JS.

Presets → `schedule:` string:
- once     → `<date>T<HH>:<MM>:00`   (ISO datetime, one-shot)
- daily    → `M H * * *`
- weekdays → `M H * * 1-5`
- weekly   → `M H * * <dow>`         (dow 0-6, 0 = Sunday, cron convention)

`describe_schedule` reverse-maps any of those four back to structured fields plus
a human label and the next fire time; anything else round-trips as `custom`
(read-only in the editor) so a hand-written cron isn't silently mangled.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from boot import timers as T


# 0 = Sunday, matching cron's day-of-week numbering (croniter accepts 0 and 7
# for Sunday; we always emit 0).
DAY_NAMES = [
    "Sunday",
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
]


def _parse_hhmm(time: str) -> tuple[int, int]:
    """Parse a browser `<input type=time>` value ("HH:MM") into (hour, minute)."""
    if not isinstance(time, str) or ":" not in time:
        raise ValueError(f"invalid time {time!r}, expected HH:MM")
    hh, _, mm = time.partition(":")
    try:
        hour, minute = int(hh), int(mm)
    except ValueError as e:
        raise ValueError(f"invalid time {time!r}, expected HH:MM") from e
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"time out of range: {time!r}")
    return hour, minute


def build_schedule(
    repeat: str,
    time: str,
    dow: Optional[int] = None,
    date: Optional[str] = None,
) -> str:
    """Turn a preset + structured fields into a `schedule:` string.

    Raises ValueError on an unknown preset or a missing/invalid field, so the
    server maps a bad request to a 400 rather than writing a broken spec.
    """
    hour, minute = _parse_hhmm(time)
    if repeat == "once":
        if not date:
            raise ValueError("a one-shot task needs a date")
        # Validate the date shape by round-tripping it through fromisoformat.
        try:
            datetime.fromisoformat(f"{date}T{hour:02d}:{minute:02d}:00")
        except ValueError as e:
            raise ValueError(f"invalid date {date!r}, expected YYYY-MM-DD") from e
        return f"{date}T{hour:02d}:{minute:02d}:00"
    if repeat == "daily":
        return f"{minute} {hour} * * *"
    if repeat == "weekdays":
        return f"{minute} {hour} * * 1-5"
    if repeat == "weekly":
        if dow is None:
            raise ValueError("a weekly task needs a day of week")
        try:
            day = int(dow)
        except (TypeError, ValueError) as e:
            raise ValueError(f"invalid day of week {dow!r}") from e
        if not 0 <= day <= 6:
            raise ValueError(f"day of week out of range: {dow!r} (expected 0-6)")
        return f"{minute} {hour} * * {day}"
    raise ValueError(f"unknown repeat {repeat!r}")


def _next_fire(schedule: str) -> Optional[str]:
    """Local ISO time of the next fire, or None (one-shot already past / unparsable)."""
    try:
        nxt, _ = T.parse_schedule(schedule, datetime.now())
    except Exception:
        return None
    return nxt.isoformat(timespec="seconds") if nxt is not None else None


def _custom(schedule: str, time: Optional[str]) -> dict:
    return {
        "repeat": "custom",
        "time": time,
        "dow": None,
        "date": None,
        "label": f"Custom · {schedule}",
        "next_fire": _next_fire(schedule),
    }


def describe_schedule(schedule) -> dict:
    """Reverse-map a `schedule:` string to structured fields + label + next fire.

    Returns `{repeat, time, dow, date, label, next_fire}`. `repeat` is one of
    once/daily/weekdays/weekly, or `custom` for any shape the presets don't
    emit (shown read-only in the editor). Because the presets only ever produce
    those four shapes, `describe_schedule(build_schedule(...))` round-trips.
    """
    text = str(schedule).strip()

    # One-shot: an ISO datetime parses; a cron expression does not.
    try:
        dt = datetime.fromisoformat(text)
    except (ValueError, TypeError):
        dt = None
    if dt is not None:
        return {
            "repeat": "once",
            "time": dt.strftime("%H:%M"),
            "dow": None,
            "date": dt.date().isoformat(),
            "label": f"Once · {dt.strftime('%b %-d, %Y · %H:%M')}",
            "next_fire": _next_fire(text),
        }

    parts = text.split()
    if len(parts) != 5:
        return _custom(text, None)
    minute_s, hour_s, dom, mon, dw = parts
    try:
        hour, minute = int(hour_s), int(minute_s)
    except ValueError:
        return _custom(text, None)
    time = f"{hour:02d}:{minute:02d}"

    # Only the "every day, at H:M" family maps back to a preset; anything that
    # constrains day-of-month or month is a custom cron.
    if dom != "*" or mon != "*":
        return _custom(text, time)

    next_fire = _next_fire(text)
    if dw == "*":
        return {
            "repeat": "daily",
            "time": time,
            "dow": None,
            "date": None,
            "label": f"Every day · {time}",
            "next_fire": next_fire,
        }
    if dw == "1-5":
        return {
            "repeat": "weekdays",
            "time": time,
            "dow": None,
            "date": None,
            "label": f"Every weekday · {time}",
            "next_fire": next_fire,
        }
    if dw.isdigit():
        day = int(dw) % 7  # cron 7 == Sunday == 0
        return {
            "repeat": "weekly",
            "time": time,
            "dow": day,
            "date": None,
            "label": f"Every {DAY_NAMES[day]} · {time}",
            "next_fire": next_fire,
        }
    return _custom(text, time)
