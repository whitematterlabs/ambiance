"""Kernel-side driver health breadcrumbs (/proc/<slug>/health.yaml).

The supervision paths in boot.main write a durable record at every lifecycle
boundary — start, crash, cancel, clean return, failed spawn — so a driver
that dies silently still leaves a tell on disk. These tests pin the file
shape and that each supervision path actually writes its breadcrumb.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from boot import driver_health as DH
from boot import main as M
from boot import processes as P


@pytest.fixture
def proc_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    proc = tmp_path / "proc"
    events = tmp_path / "events"
    home = tmp_path / "home"
    proc.mkdir()
    events.mkdir()
    home.mkdir()
    monkeypatch.setattr(P, "PROC_DIR", proc, raising=True)
    monkeypatch.setattr(P, "EVENTS_DIR", events, raising=True)
    monkeypatch.setattr(P, "HOME_DIR", home, raising=True)
    return proc


def _health(proc_root: Path, slug: str) -> dict:
    with (proc_root / slug / "health.yaml").open() as f:
        return yaml.safe_load(f) or {}


# --- the breadcrumb primitives ----------------------------------------------


def test_record_start_counts_and_keeps_bounded_ring(proc_root: Path) -> None:
    M._ensure_driver_proc("email-in")
    for i in range(DH.RECENT_STARTS_CAP + 2):
        if i:
            DH.record_exit("email-in", "crashed", "boom", now=f"2026-07-07T09:59:{i:02d}")
        DH.record_start("email-in", now=f"2026-07-07T10:00:{i:02d}")
    h = _health(proc_root, "email-in")
    assert h["starts"] == DH.RECENT_STARTS_CAP + 2
    assert h["last_start"] == f"2026-07-07T10:00:{DH.RECENT_STARTS_CAP + 1:02d}"
    # The ring is bounded — health.yaml must not grow with uptime.
    assert len(h["recent_starts"]) == DH.RECENT_STARTS_CAP
    assert h["recent_starts"][-1] == h["last_start"]


def test_loop_ring_only_collects_failure_respawns(proc_root: Path) -> None:
    """Regression (2026-07-11 'every driver is looping'): repeated kernel
    re-execs cancel every driver task and start it again; those starts landed
    in recent_starts and tripped the console's >=3-starts-in-30m loop signal
    fleet-wide. Only a start whose *preceding* exit was a failure is
    crash-loop evidence — first starts and post-cancel starts stay out."""
    M._ensure_driver_proc("email-in")
    # First-ever start: no prior exit, not loop evidence.
    DH.record_start("email-in", now="2026-07-11T02:30:00")
    # A kernel re-exec: cancelled exit, then the new kernel's start.
    DH.record_exit("email-in", "cancelled", now="2026-07-11T02:34:00")
    DH.record_start("email-in", now="2026-07-11T02:34:01")
    h = _health(proc_root, "email-in")
    assert h["recent_starts"] == []
    assert h["starts"] == 2  # the lifetime counter still sees every start
    assert h["last_start"] == "2026-07-11T02:34:01"
    # Failure respawns DO count — crash, silent return, failed spawn.
    for i, outcome in enumerate(("crashed", "returned", "failed_to_start")):
        DH.record_exit("email-in", outcome, now=f"2026-07-11T02:4{i}:00")
        DH.record_start("email-in", now=f"2026-07-11T02:4{i}:01")
    h = _health(proc_root, "email-in")
    assert h["recent_starts"] == [
        "2026-07-11T02:40:01",
        "2026-07-11T02:41:01",
        "2026-07-11T02:42:01",
    ]


def test_record_exit_writes_outcome_and_reason(proc_root: Path) -> None:
    M._ensure_driver_proc("email-in")
    DH.record_start("email-in", now="2026-07-07T10:00:00")
    DH.record_exit("email-in", "crashed", "RuntimeError('boom')", now="2026-07-07T11:00:00")
    h = _health(proc_root, "email-in")
    assert h["last_exit"] == "2026-07-07T11:00:00"
    assert h["last_exit_outcome"] == "crashed"
    assert h["last_exit_reason"] == "RuntimeError('boom')"
    # Start-side fields survive an exit write.
    assert h["starts"] == 1
    assert h["last_start"] == "2026-07-07T10:00:00"


def test_default_timestamp_carries_subsecond_precision(proc_root: Path) -> None:
    """Regression: a graceful kernel restart records the old task's cancel-exit
    and the new task's start moments apart. At second resolution those two
    genuinely-ordered events collapse to an equal string whenever they fall in
    the same second, and the web console then misreads a live driver as
    'down — task cancelled'. Sub-second precision keeps them orderable."""
    import re

    ts = DH._now_iso()
    # Fixed-width microseconds → lexicographic compare stays chronological.
    assert re.search(r"T\d\d:\d\d:\d\d\.\d{6}$", ts), ts


def test_breadcrumbs_are_noops_without_a_proc_entry(proc_root: Path) -> None:
    # Health is a breadcrumb, not a dependency: no proc dir → silently skip.
    DH.record_start("ghost")
    DH.record_exit("ghost", "crashed", "boom")
    assert not (proc_root / "ghost").exists()
    assert DH.read("ghost") == {}


# --- supervision hook points --------------------------------------------------


def test_supervise_records_start_and_crash(proc_root: Path) -> None:
    async def crash() -> None:
        raise RuntimeError("boom")

    M._driver_tasks.clear()
    asyncio.run(M._supervise_driver("email-in", crash()))
    h = _health(proc_root, "email-in")
    assert h["starts"] == 1
    assert h["last_exit_outcome"] == "crashed"
    assert "boom" in h["last_exit_reason"]
    assert P.read_status("email-in") == "failed"


def test_supervise_records_silent_clean_return(proc_root: Path) -> None:
    """A driver coroutine that just returns leaves /proc status 'running' —
    the health breadcrumb is the only durable record it's gone. This is the
    exact failure shape of the backfill-that-never-ran class."""

    async def quiet_exit() -> None:
        return None

    M._driver_tasks.clear()
    asyncio.run(M._supervise_driver("email-in", quiet_exit()))
    h = _health(proc_root, "email-in")
    assert h["last_exit_outcome"] == "returned"
    # The tell: exited at/after its last start, while status still says running.
    assert h["last_exit"] >= h["last_start"]
    assert P.read_status("email-in") == "running"


def test_supervise_records_cancel(proc_root: Path) -> None:
    async def scenario() -> None:
        task = asyncio.create_task(
            M._supervise_driver("email-in", asyncio.Event().wait())
        )
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    M._driver_tasks.clear()
    asyncio.run(scenario())
    h = _health(proc_root, "email-in")
    assert h["last_exit_outcome"] == "cancelled"
    assert P.read_status("email-in") == "cancelled"


def test_reconcile_records_failed_to_start(
    proc_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def bad_factory():
        raise ImportError("no module named drivers.broken")

    monkeypatch.setattr(
        M,
        "_discover_driver_specs",
        lambda: (("broken-in", bad_factory),),
        raising=True,
    )
    M._driver_tasks.clear()
    asyncio.run(M._reconcile_drivers())
    h = _health(proc_root, "broken-in")
    assert h["last_exit_outcome"] == "failed_to_start"
    assert "no module named" in h["last_exit_reason"]
    assert P.read_status("broken-in") == "failed"
