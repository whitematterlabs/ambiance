"""Cron/timer procs rest at `scheduled`, not `running`.

A pure timer proc (has `schedule:` or `deadline:`, no live background
subprocess) is an armed timer in the kernel's heap — nothing is executing.
It must read `scheduled` so surfaces and humans can tell it apart from a
live `running` background service. Only a `run:`-only service is `running`.

This is the load-bearing invariant: `scheduled` joins `running` as an
"active / in-heap" status everywhere the kernel decides what to re-arm,
fire, or preserve across a restart.

Isolation: monkeypatch the cached PROC_DIR globals (per conftest's
`live_dir` pattern). Never reload modules — that leaks global state into
later tests in the same process.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from boot import processes as P
from boot import timers as T


@pytest.fixture()
def proc_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    proc = tmp_path / "home" / "proc"
    proc.mkdir(parents=True)
    monkeypatch.setattr(P, "PROC_DIR", proc, raising=True)
    # timers.py binds PROC_DIR by value at import; redirect its copy too.
    monkeypatch.setattr(T, "PROC_DIR", proc, raising=True)
    return proc


def test_cron_spec_spawns_scheduled(proc_dir) -> None:
    P.spawn("nightly", {"schedule": "0 3 * * *", "parent": 3})
    assert P.read_status("nightly") == "scheduled"


def test_deadline_spec_spawns_scheduled(proc_dir) -> None:
    P.spawn("remind", {"deadline": "2030-01-01T00:00:00", "parent": 3})
    assert P.read_status("remind") == "scheduled"


def test_background_service_spawns_running(proc_dir) -> None:
    P.spawn("proxy", {"run": "litellm --port 4000"})
    assert P.read_status("proxy") == "running"


def test_deadline_capped_service_spawns_running(proc_dir) -> None:
    """`run:` + `deadline:` (no `schedule:`) is a live background service whose
    runtime is merely capped — it must spawn `running` so proc_watcher actually
    supervises it. The deadline is only an expiry cap, not a timer to rest on."""
    P.spawn("capped", {"run": "long-poll", "deadline": "2030-01-01T00:00:00"})
    assert P.read_status("capped") == "running"


def test_plain_pai_spawns_running(proc_dir) -> None:
    P.spawn("helper", {"kind": "pai", "parent": 1})
    assert P.read_status("helper") == "running"


def test_scheduled_is_active_not_terminal() -> None:
    assert "scheduled" in P.VALID_STATUSES
    assert "scheduled" in P.ACTIVE_STATUSES
    assert "running" in P.ACTIVE_STATUSES
    assert "scheduled" not in P.TERMINAL_STATUSES


def test_rebuild_rearms_scheduled_cron(proc_dir) -> None:
    """A `scheduled` cron must be re-armed onto the timer heap at boot —
    `running` is no longer the only signal the rebuild keys off."""
    P.spawn("nightly", {"schedule": "0 3 * * *", "parent": 3})
    assert P.read_status("nightly") == "scheduled"
    heap = T.rebuild_from_proc()
    assert any(e.slug == "nightly" for e in heap)


def test_list_active_includes_running_and_scheduled(proc_dir) -> None:
    P.spawn("nightly", {"schedule": "0 3 * * *", "parent": 3})
    P.spawn("proxy", {"run": "litellm --port 4000"})
    active = set(P.list_active_procs())
    assert {"nightly", "proxy"} <= active
