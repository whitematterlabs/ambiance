"""Web-side driver health aggregation (the console's Drivers panel).

pai_web.driver_health folds together what is already on disk — manifest
process lists + optional `health:` thresholds, /proc status, the kernel's
health.yaml breadcrumbs, and /sys/drivers mtimes — into one classified row
per driver process. These tests pin the threshold parsing, the discovery
walk, the classification ladder, and the change-gated hub broadcast.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from boot import driver_health as B
from boot import paths as PA
from boot import processes as P
from usr.libexec.web.pai_web import driver_health as dh
from usr.libexec.web.pai_web import hub as H


NOW = time.time()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds")


@pytest.fixture
def fhs(live_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway FHS: drivers manifests + /sys/drivers under the live_dir
    PAI_ROOT (live_dir already redirects PROC_DIR / paths.PAI_ROOT)."""
    drivers = tmp_path / "usr" / "lib" / "drivers"
    drivers.mkdir(parents=True)
    (tmp_path / "sys" / "drivers").mkdir(parents=True)
    monkeypatch.setattr(PA, "usr_lib_drivers", lambda: drivers, raising=True)
    return tmp_path


def _manifest(fhs: Path, driver: str, text: str, sub: str = "") -> None:
    d = fhs / "usr" / "lib" / "drivers" / driver / sub if sub else fhs / "usr" / "lib" / "drivers" / driver
    d.mkdir(parents=True, exist_ok=True)
    (d / "events.yaml").write_text(text)


def _driver_proc(slug: str, active: bool = True, status: str = "running") -> None:
    P.spawn(slug, {"kind": "driver", "active": active})
    (P.PROC_DIR / slug / "status").write_text(f"{status}\n")


def _sys_file(fhs: Path, driver: str, name: str, age_s: float) -> None:
    d = fhs / "sys" / "drivers" / driver
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    f.write_text("state\n")
    os.utime(f, (NOW - age_s, NOW - age_s))


def _row(rows: list[dict], slug: str) -> dict:
    return next(r for r in rows if r["slug"] == slug)


# --- threshold parsing --------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "secs"),
    [
        (90, 90),
        ("90", 90),
        ("90s", 90),
        ("15m", 900),
        ("6h", 21600),
        ("3d", 259200),
        ("1.5h", 5400),
        (None, None),
        (True, None),
        ("soon", None),
        (0, None),
        (-5, None),
    ],
)
def test_parse_duration(raw, secs) -> None:
    assert dh.parse_duration(raw) == secs


# --- discovery ----------------------------------------------------------------


def test_discover_thresholds_and_subdriver_names(fhs: Path) -> None:
    _manifest(
        fhs,
        "imessage",
        "driver: imessage\n"
        "health:\n"
        "  stale_after: 6h\n"
        "processes:\n"
        "  - slug: imessage-in\n"
        "    module: drivers.imessage.inbound\n"
        "  - slug: imessage-out\n"
        "    module: drivers.imessage.outbound\n"
        "    health:\n"
        "      stale_after: 2h\n",
    )
    # A sub-driver manifest (email/macmail) keys /sys state off the top dir.
    _manifest(
        fhs,
        "email",
        "driver: email\nprocesses:\n  - slug: email-in\n    module: m\n",
        sub="macmail",
    )
    descs = {d["slug"]: d for d in dh.discover_processes()}
    assert descs["imessage-in"]["stale_after_s"] == 6 * 3600  # driver-wide
    assert descs["imessage-out"]["stale_after_s"] == 2 * 3600  # per-process override
    assert descs["email-in"]["stale_after_s"] == dh.DEFAULT_STALE_AFTER_S  # default
    assert descs["email-in"]["driver"] == "email"  # top dir, not "macmail"


# --- classification ladder ------------------------------------------------------


def test_running_with_recent_activity_is_ok(fhs: Path) -> None:
    _manifest(fhs, "email", "driver: email\nprocesses:\n  - slug: email-in\n    module: m\n")
    _driver_proc("email-in")
    B.record_start("email-in", now=_iso(NOW - 3600))
    _sys_file(fhs, "email", "cursor.yaml", age_s=120)
    row = _row(dh.read_rows(now=NOW), "email-in")
    assert row["state"] == "ok"
    assert row["starts"] == 1
    assert row["last_activity"] == pytest.approx(NOW - 120, abs=2)


def test_running_but_silent_past_threshold_is_stale(fhs: Path) -> None:
    _manifest(
        fhs,
        "imessage",
        "driver: imessage\nhealth:\n  stale_after: 6h\n"
        "processes:\n  - slug: imessage-in\n    module: m\n",
    )
    _driver_proc("imessage-in")
    B.record_start("imessage-in", now=_iso(NOW - 8 * 3600))
    _sys_file(fhs, "imessage", "cursor.yaml", age_s=7 * 3600)
    row = _row(dh.read_rows(now=NOW), "imessage-in")
    assert row["state"] == "stale"
    assert "no activity since" in row["state_reason"]


def test_fresh_start_is_not_stale_without_sys_activity(fhs: Path) -> None:
    # last_activity falls back to the task's own start, so a just-started
    # driver with no /sys state yet doesn't open yellow.
    _manifest(fhs, "email", "driver: email\nprocesses:\n  - slug: email-in\n    module: m\n")
    _driver_proc("email-in")
    B.record_start("email-in", now=_iso(NOW - 60))
    row = _row(dh.read_rows(now=NOW), "email-in")
    assert row["state"] == "ok"


def test_active_but_failed_status_is_down(fhs: Path) -> None:
    _manifest(fhs, "email", "driver: email\nprocesses:\n  - slug: email-in\n    module: m\n")
    _driver_proc("email-in", status="failed")
    B.record_start("email-in", now=_iso(NOW - 3600))
    B.record_exit("email-in", "crashed", "RuntimeError('boom')", now=_iso(NOW - 300))
    row = _row(dh.read_rows(now=NOW), "email-in")
    assert row["state"] == "down"
    assert "boom" in row["state_reason"]


def test_silent_clean_return_is_down_despite_running_status(fhs: Path) -> None:
    """The email-backfill failure class: coroutine returned, /proc still says
    running, nothing crashed. The exit-after-start breadcrumb must go red."""
    _manifest(fhs, "email", "driver: email\nprocesses:\n  - slug: email-in\n    module: m\n")
    _driver_proc("email-in", status="running")
    B.record_start("email-in", now=_iso(NOW - 7200))
    B.record_exit("email-in", "returned", now=_iso(NOW - 7100))
    _sys_file(fhs, "email", "cursor.yaml", age_s=60)  # even with fresh-ish state
    row = _row(dh.read_rows(now=NOW), "email-in")
    assert row["state"] == "down"
    assert "returned" in row["state_reason"]


def test_same_second_restart_is_not_falsely_down(fhs: Path) -> None:
    """Regression (ax-in 'always cancelled'): on a graceful kernel restart the
    old task's cancel-exit and the new task's start land in the same wall-clock
    second. With sub-second precision the new start still sorts *after* the old
    exit, so the running driver classifies ok — not 'down — task cancelled'."""
    _manifest(fhs, "ax", "driver: ax\nprocesses:\n  - slug: ax-in\n    module: m\n")
    _driver_proc("ax-in", status="running")
    base = datetime.fromtimestamp(NOW - 120).replace(microsecond=0)
    old_exit = base.replace(microsecond=100000).isoformat(timespec="microseconds")
    new_start = base.replace(microsecond=500000).isoformat(timespec="microseconds")
    B.record_start("ax-in", now=new_start)
    B.record_exit("ax-in", "cancelled", now=old_exit)
    _sys_file(fhs, "ax", "cursor.yaml", age_s=30)
    row = _row(dh.read_rows(now=NOW), "ax-in")
    assert row["state"] == "ok", row["state_reason"]


def test_kernel_reexec_storm_is_not_looping(fhs: Path) -> None:
    """Regression (2026-07-11): root re-exec'd the kernel 4x in 15 minutes
    while wiring a new driver; every healthy driver's task was cancelled and
    restarted each time and the whole panel went red 'looping'. Deliberate
    cancel→start cycles are not crash loops."""
    _manifest(fhs, "email", "driver: email\nprocesses:\n  - slug: email-in\n    module: m\n")
    _driver_proc("email-in")
    B.record_start("email-in", now=_iso(NOW - 1200))  # first boot
    for i in range(dh.LOOP_THRESHOLD + 1):
        B.record_exit("email-in", "cancelled", now=_iso(NOW - 900 + i * 180 - 1))
        B.record_start("email-in", now=_iso(NOW - 900 + i * 180))
    _sys_file(fhs, "email", "cursor.yaml", age_s=30)
    row = _row(dh.read_rows(now=NOW), "email-in")
    assert row["state"] == "ok", row["state_reason"]


def test_dense_restarts_classify_as_looping(fhs: Path) -> None:
    _manifest(fhs, "email", "driver: email\nprocesses:\n  - slug: email-in\n    module: m\n")
    _driver_proc("email-in")
    for i in range(dh.LOOP_THRESHOLD):
        B.record_exit("email-in", "crashed", "boom", now=_iso(NOW - 600 + i * 60 - 1))
        B.record_start("email-in", now=_iso(NOW - 600 + i * 60))
    _sys_file(fhs, "email", "cursor.yaml", age_s=30)
    row = _row(dh.read_rows(now=NOW), "email-in")
    assert row["state"] == "looping"
    assert "failure respawns in" in row["state_reason"]


def test_disabled_driver_is_off_not_alarming(fhs: Path) -> None:
    _manifest(fhs, "voice", "driver: voice\nprocesses:\n  - slug: voice-in\n    module: m\n")
    _driver_proc("voice-in", active=False, status="stopped")
    row = _row(dh.read_rows(now=NOW), "voice-in")
    assert row["state"] == "off"


def test_manifest_process_without_proc_entry_is_down(fhs: Path) -> None:
    # Installed but never spawned (kernel hasn't rebooted since install, or
    # discovery is broken) — exactly the silence that must show red.
    _manifest(fhs, "email", "driver: email\nprocesses:\n  - slug: email-in\n    module: m\n")
    row = _row(dh.read_rows(now=NOW), "email-in")
    assert row["state"] == "down"
    assert "never started" in row["state_reason"]


def test_kernel_watchers_are_included(fhs: Path) -> None:
    slugs = {r["slug"] for r in dh.read_rows(now=NOW)}
    assert {"proc-watcher", "doc-watcher"} <= slugs


# --- hub broadcast --------------------------------------------------------------


def test_hub_drivers_broadcast_is_gated_on_change(fhs: Path) -> None:
    """The safety poll pokes the drivers recompute twice a second; identical
    rows must not re-broadcast, while a real state flip must."""

    class _FakeSub:
        def __init__(self) -> None:
            self.sent: list[dict] = []

        def send(self, msg: dict) -> None:
            self.sent.append(msg)

    _manifest(fhs, "email", "driver: email\nprocesses:\n  - slug: email-in\n    module: m\n")
    _driver_proc("email-in")
    B.record_start("email-in", now=_iso(time.time()))

    hub = H.Hub()
    sub = _FakeSub()
    hub.add(sub)

    def drivers_msgs() -> list[dict]:
        return [m for m in sub.sent if m.get("type") == "drivers"]

    hub._recompute_drivers(broadcast=True)  # [] -> rows: a real change
    hub._recompute_drivers(broadcast=True)  # identical: gated
    assert len(drivers_msgs()) == 1
    assert _row(drivers_msgs()[0]["drivers"], "email-in")["state"] == "ok"

    (P.PROC_DIR / "email-in" / "status").write_text("failed\n")
    B.record_exit("email-in", "crashed", "boom")
    hub._recompute_drivers(broadcast=True)  # state flip: broadcast
    assert len(drivers_msgs()) == 2
    assert _row(drivers_msgs()[1]["drivers"], "email-in")["state"] == "down"

    # And the snapshot carries the same rows for a fresh client.
    snap_rows = hub._drivers
    assert _row(snap_rows, "email-in")["state"] == "down"
