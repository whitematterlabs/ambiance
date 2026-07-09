"""Crash-loop guard: `restart: always` must not fork-storm the kernel.

A service that exits within supervisor._STABLE_SECS is a rapid exit; each
consecutive one restarts with exponential backoff and once _CRASH_BUDGET are
spent the proc resolves `failed` and a crash_loop event nudges the parent.
A run that survives _STABLE_SECS clears the counter. (2026-07-08: a spec with
a nonexistent node path respawned ~220×/s for 45 minutes.)"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from boot import processes as P
from boot import supervisor as S


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
    monkeypatch.setattr(S, "_BACKOFF_BASE", 0.01, raising=True)
    monkeypatch.setattr(S, "_BACKOFF_MAX", 0.05, raising=True)
    S._handles.clear()
    S._crashes.clear()
    yield proc
    S._handles.clear()
    S._crashes.clear()


def _events(events_dir: Path) -> list[dict]:
    out = []
    for f in sorted(events_dir.glob("*.yaml")):
        with f.open() as fh:
            out.append(yaml.safe_load(fh) or {})
    return out


async def _drain(slug: str, timeout: float = 10.0) -> None:
    """Wait until the supervisor is done with slug (no live handle/waiter)."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        handle = S._handles.get(slug)
        if handle is None and P.read_status(slug) != "running":
            return
        await asyncio.sleep(0.02)
        if handle is not None:
            try:
                await asyncio.wait_for(asyncio.shield(handle.waiter), 0.2)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
    raise AssertionError(f"supervisor never settled for {slug}")


def test_instant_crash_gives_up_after_budget(proc_root: Path) -> None:
    """An instantly-dying service restarts _CRASH_BUDGET times, then fails."""
    slug = "crashy"
    spec = {"run": ["/bin/sh", "-c", "exit 7"], "restart": "always", "parent": 1}
    P.spawn(slug, spec)
    P.mark_running(slug)

    async def scenario():
        await S.start(slug, spec)
        await _drain(slug)

    asyncio.run(scenario())

    assert P.read_status(slug) == "failed"
    log = (proc_root / slug / "log.md").read_text()
    assert "giving up" in log
    # _CRASH_BUDGET spawns total: budget-1 restarts after the first start.
    assert log.count("kernel: subprocess started") == S._CRASH_BUDGET
    assert slug not in S._crashes

    events = _events(P.EVENTS_DIR)
    crash = [e for e in events if e.get("kind") == "crash_loop"]
    assert len(crash) == 1
    assert crash[0]["slug"] == slug
    assert crash[0]["rc"] == 7
    assert crash[0]["failures"] == S._CRASH_BUDGET
    assert crash[0]["parent"] == 1
    # The failed resolution must not also nudge the parent (double nudge).
    resolved = [e for e in events if e.get("kind") == "proc_resolved"]
    assert all("parent" not in e for e in resolved)


def test_stable_run_resets_crash_counter(proc_root: Path, monkeypatch) -> None:
    """A run longer than _STABLE_SECS clears the consecutive-crash count."""
    monkeypatch.setattr(S, "_STABLE_SECS", 0.0, raising=True)
    slug = "flaky"
    spec = {"run": ["/bin/sh", "-c", "exit 1"], "restart": "on-failure"}
    P.spawn(slug, spec)
    P.mark_running(slug)

    async def scenario():
        # Pre-load a nearly-spent budget; a "stable" exit must clear it
        # rather than tipping into give-up.
        S._crashes[slug] = S._CRASH_BUDGET - 1
        await S.start(slug, spec)
        handle = S._handles[slug]
        await asyncio.wait_for(asyncio.shield(handle.waiter), 5)
        # With _STABLE_SECS=0 every exit counts as stable → restart, reset.
        assert S._crashes.get(slug) is None
        # Stop the restarted service so the test doesn't loop forever.
        P.resolve(slug, "cancelled")
        await S.stop(slug)

    asyncio.run(scenario())


def test_stop_during_backoff_stays_down(proc_root: Path, monkeypatch) -> None:
    """Externally resolving the proc during a backoff sleep cancels restart."""
    monkeypatch.setattr(S, "_BACKOFF_BASE", 0.3, raising=True)
    monkeypatch.setattr(S, "_BACKOFF_MAX", 0.3, raising=True)
    slug = "stopped-mid-backoff"
    spec = {"run": ["/bin/sh", "-c", "exit 1"], "restart": "always"}
    P.spawn(slug, spec)
    P.mark_running(slug)

    async def scenario():
        await S.start(slug, spec)
        waiter = S._handles[slug].waiter
        # Let the first exit land and enter the backoff sleep, then resolve.
        await asyncio.sleep(0.15)
        P.resolve(slug, "cancelled")
        await asyncio.wait_for(asyncio.shield(waiter), 5)

    asyncio.run(scenario())

    assert P.read_status(slug) == "cancelled"
    assert slug not in S._handles
    log = (proc_root / slug / "log.md").read_text()
    assert log.count("kernel: subprocess started") == 1
