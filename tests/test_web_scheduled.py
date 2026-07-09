"""Scheduled-tasks CRUD on the web surface.

A scheduled task is a paicron proc — no new store. These cover the three
actions the console calls: `add_scheduled` writes a filtered-in `owner-task`
spec (schedule/description/parent, no run), `list_scheduled` returns it with the
target PAI's slug resolved and never surfaces non-owner crons, and
`remove_scheduled` flips status so it drops out.
"""

from __future__ import annotations

import pytest

from boot import processes as P
from usr.libexec.web.pai_web import actions


@pytest.fixture()
def fleet(live_dir):
    """A running fleet PAI with an invariant pid the tasks can target."""
    P.spawn("assistant", {"kind": "pai", "pid": 5})
    return "assistant"


def test_add_scheduled_writes_owner_task_spec(fleet):
    row = actions.add_scheduled("assistant", "daily", "09:00", instruction="summarize email")
    slug = row["slug"]
    assert slug.startswith("owner-task-")

    spec = P.read_spec(slug)
    assert spec["schedule"] == "0 9 * * *"
    assert spec["description"] == "summarize email"
    assert spec["parent"] == 5
    # No `run:` — that's what routes the fire to a PAI nudge, not a subprocess.
    assert "run" not in spec
    # A pure `schedule:` proc rests at `scheduled`.
    assert P.read_status(slug) == "scheduled"

    # The returned row is already the projected shape (resolved PAI + label).
    assert row["pai"] == "assistant"
    assert row["repeat"] == "daily"
    assert row["label"] == "Every day · 09:00"


def test_add_scheduled_requires_running_pai(fleet):
    with pytest.raises(ValueError):
        actions.add_scheduled("ghost", "daily", "09:00", instruction="x")


def test_add_scheduled_requires_instruction(fleet):
    with pytest.raises(ValueError):
        actions.add_scheduled("assistant", "daily", "09:00", instruction="   ")


def test_add_scheduled_rejects_past_oneshot(fleet):
    with pytest.raises(ValueError):
        actions.add_scheduled(
            "assistant", "once", "09:00", date="2000-01-01", instruction="late"
        )


def test_list_scheduled_returns_owner_tasks_only(fleet):
    actions.add_scheduled("assistant", "daily", "09:00", instruction="owner job")
    # A PAI-internal cron (not base `owner-task`) and the PAI proc itself must
    # never appear in the panel.
    P.spawn("reminder-2026-01-01", {"schedule": "0 3 * * *", "parent": 5})

    rows = actions.list_scheduled()
    assert len(rows) == 1
    assert rows[0]["pai"] == "assistant"
    assert rows[0]["instruction"] == "owner job"


def test_remove_scheduled_drops_from_list(fleet):
    row = actions.add_scheduled("assistant", "daily", "09:00", instruction="temp")
    assert actions.list_scheduled()

    out = actions.remove_scheduled(row["slug"])
    assert out["status"] == "cancelled"
    assert actions.list_scheduled() == []


def test_remove_scheduled_is_idempotent(fleet):
    out = actions.remove_scheduled("owner-task-does-not-exist")
    assert out["status"] == "missing"


def test_update_scheduled_recreates_with_new_slug(fleet):
    row = actions.add_scheduled("assistant", "daily", "09:00", instruction="v1")
    old_slug = row["slug"]

    updated = actions.update_scheduled(
        old_slug, "assistant", "weekly", "10:30", dow=2, instruction="v2"
    )
    assert updated["slug"] != old_slug
    assert updated["repeat"] == "weekly"
    assert updated["instruction"] == "v2"

    # Old task is cancelled (gone from the panel); exactly one owner task remains.
    assert P.read_status(old_slug) == "cancelled"
    rows = actions.list_scheduled()
    assert len(rows) == 1
    assert rows[0]["slug"] == updated["slug"]
