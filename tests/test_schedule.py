"""Schedule semantics — the single source of truth for owner scheduled tasks.

`build_schedule` emits exactly four shapes; `describe_schedule` reverse-maps
each back to structured fields + a label + a future next-fire. The round-trip
is the load-bearing invariant: the console edits a task by describing its stored
`schedule:` and re-submitting the fields, so describe∘build must be identity on
the presets.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from usr.libexec.web.pai_web import schedule as S


def test_build_daily():
    assert S.build_schedule("daily", "09:00") == "0 9 * * *"


def test_build_weekdays():
    assert S.build_schedule("weekdays", "08:30") == "30 8 * * 1-5"


def test_build_weekly():
    assert S.build_schedule("weekly", "17:05", dow=1) == "5 17 * * 1"


def test_build_once():
    assert S.build_schedule("once", "09:00", date="2099-01-02") == "2099-01-02T09:00:00"


def test_build_rejects_unknown_repeat():
    with pytest.raises(ValueError):
        S.build_schedule("hourly", "09:00")


def test_build_rejects_bad_time():
    with pytest.raises(ValueError):
        S.build_schedule("daily", "9am")
    with pytest.raises(ValueError):
        S.build_schedule("daily", "25:00")


def test_build_weekly_needs_dow():
    with pytest.raises(ValueError):
        S.build_schedule("weekly", "09:00")


def test_build_weekly_rejects_out_of_range_dow():
    with pytest.raises(ValueError):
        S.build_schedule("weekly", "09:00", dow=9)


def test_build_once_needs_date():
    with pytest.raises(ValueError):
        S.build_schedule("once", "09:00")


@pytest.mark.parametrize(
    "repeat,time,dow,date",
    [
        ("daily", "09:00", None, None),
        ("weekdays", "08:30", None, None),
        ("weekly", "17:05", 3, None),
        ("once", "09:00", None, "2099-01-02"),
    ],
)
def test_describe_round_trips_presets(repeat, time, dow, date):
    schedule = S.build_schedule(repeat, time, dow=dow, date=date)
    desc = S.describe_schedule(schedule)
    assert desc["repeat"] == repeat
    assert desc["time"] == time
    assert desc["dow"] == dow
    assert desc["date"] == date


def test_describe_labels():
    assert S.describe_schedule("0 9 * * *")["label"] == "Every day · 09:00"
    assert S.describe_schedule("30 8 * * 1-5")["label"] == "Every weekday · 08:30"
    assert S.describe_schedule("5 17 * * 1")["label"] == "Every Monday · 17:05"
    assert S.describe_schedule("2099-01-02T09:00:00")["label"].startswith("Once · Jan 2, 2099")


def test_describe_next_fire_is_future():
    for schedule in ("0 9 * * *", "30 8 * * 1-5", "5 17 * * 1"):
        nxt = S.describe_schedule(schedule)["next_fire"]
        assert nxt is not None
        assert datetime.fromisoformat(nxt) > datetime.now()


def test_describe_once_future_next_fire():
    future = (datetime.now() + timedelta(days=2)).replace(microsecond=0)
    schedule = future.isoformat()
    desc = S.describe_schedule(schedule)
    assert desc["repeat"] == "once"
    assert desc["next_fire"] is not None
    assert datetime.fromisoformat(desc["next_fire"]) > datetime.now()


def test_describe_once_past_has_no_next_fire():
    past = (datetime.now() - timedelta(days=2)).replace(microsecond=0)
    desc = S.describe_schedule(past.isoformat())
    assert desc["repeat"] == "once"
    assert desc["next_fire"] is None


def test_describe_cron_sunday_seven_normalizes_to_zero():
    # croniter accepts 7 for Sunday; build only ever emits 0, so describe must
    # fold 7 back to 0 for a clean round-trip on hand-written specs.
    desc = S.describe_schedule("0 9 * * 7")
    assert desc["repeat"] == "weekly"
    assert desc["dow"] == 0
    assert desc["label"] == "Every Sunday · 09:00"


def test_describe_unknown_shape_is_custom():
    # A day-of-month constraint is not one of the four presets → read-only custom.
    desc = S.describe_schedule("0 9 1 * *")
    assert desc["repeat"] == "custom"
    assert desc["label"] == "Custom · 0 9 1 * *"
