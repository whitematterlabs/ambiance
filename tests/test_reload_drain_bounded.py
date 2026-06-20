"""kernel:reload_config must never hang the single-threaded event loop.

Regression for the 2026-06-18 wedge: root held its per-PAI lock for ~12 min
running a driver install script inside its turn. A `paictl stop/start` fired
`kernel:reload_config`, whose nudge-drain acquired *every* per-PAI lock with no
timeout — so it parked on root's held lock forever. The main loop is strictly
serial (it awaits one event handler before consuming the next), so every queued
event after that — including a later `kernel:restart` — starved unconsumed until
a SIGTERM cleanly killed the kernel.

`_handle_restart` already drains with a bounded `asyncio.wait_for`; this asserts
`_handle_reload_config` does the same.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from boot import main as M


def test_reload_config_drain_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc = tmp_path / "proc"
    events = tmp_path / "events"
    home = tmp_path / "home"
    for d in (proc, events, home):
        d.mkdir()
    monkeypatch.setattr(M.P, "PROC_DIR", proc, raising=True)
    monkeypatch.setattr(M.P, "EVENTS_DIR", events, raising=True)
    monkeypatch.setattr(M.P, "HOME_DIR", home, raising=True)

    async def _noop_async(*a, **k):
        return None

    # Stub out the heavy critical-section work so only the drain is exercised.
    monkeypatch.setattr(M, "_reconcile_drivers", _noop_async, raising=True)
    monkeypatch.setattr(M.C, "reconcile_from_config", lambda *a, **k: None, raising=True)
    monkeypatch.setattr(M.C, "load_config", lambda *a, **k: {}, raising=True)
    monkeypatch.setattr(M.litellm_proxy, "reconcile", _noop_async, raising=True)
    # raising=False so the test fails for the RIGHT reason before the fix lands:
    # without the bounded drain the constant is simply unread and the call hangs.
    monkeypatch.setattr(M, "_RELOAD_DRAIN_TIMEOUT", 0.1, raising=False)

    M._pai_locks.clear()

    async def scenario() -> None:
        held = asyncio.Lock()
        M._pai_locks[1] = held
        await held.acquire()  # PAI 1 is mid-turn, holding its lock
        try:
            # Must return promptly despite the held lock. With an unbounded
            # drain this awaits forever and wait_for raises TimeoutError.
            await asyncio.wait_for(M._handle_reload_config(), timeout=2.0)
        finally:
            held.release()
            M._pai_locks.clear()

    asyncio.run(scenario())
