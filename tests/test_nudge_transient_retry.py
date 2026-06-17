"""Transient provider errors retry the turn once, then give up cleanly."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from boot import nudge as N
from boot import processes as P


@pytest.fixture(autouse=True)
def _reset(live_dir: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(N, "_pai_locks", {}, raising=True)
    monkeypatch.setattr(N, "HOME_DIR", P.HOME_DIR, raising=True)
    monkeypatch.setattr(N, "PROC_DIR", P.PROC_DIR, raising=True)
    monkeypatch.setattr(N, "_TRANSIENT_RETRY_DELAY", 0, raising=True)  # no real sleep


def _spawn(slug: str, *, pid: int, **extra) -> None:
    P.spawn_pai(pid=pid, slug=slug, description=f"{slug} test", extra=extra or None)


def _run(fake, *, to: int):
    import boot.llm as L
    orig = L.run_turn
    L.run_turn = fake  # type: ignore[assignment]
    try:
        asyncio.run(N.nudge(reason="hello", to=to, from_kind="kernel"))
    finally:
        L.run_turn = orig  # type: ignore[assignment]


def test_transient_error_retries_once_then_succeeds(live_dir: Path) -> None:
    _spawn("delta", pid=13)
    calls: list[str] = []

    async def fake_run_turn(system, user, history=None, env=None, *, provider=None, model=None, set_status=None):
        calls.append("call")
        if len(calls) == 1:
            raise RuntimeError("Request timed out or interrupted")  # transient
        return ("ok", list(history or []) + [
            {"role": "user", "content": user},
            {"role": "assistant", "content": "ok"},
        ])

    _run(fake_run_turn, to=13)
    assert calls == ["call", "call"]  # retried exactly once, then succeeded


def test_transient_error_twice_gives_up_without_storm(live_dir: Path) -> None:
    _spawn("epsilon", pid=14)
    calls: list[str] = []

    async def fake_run_turn(system, user, history=None, env=None, *, provider=None, model=None, set_status=None):
        calls.append("call")
        raise RuntimeError("connection error")  # transient, every time

    _run(fake_run_turn, to=14)  # must not raise
    assert calls == ["call", "call"]  # exactly two attempts, no infinite loop


def test_non_transient_error_does_not_retry(live_dir: Path) -> None:
    _spawn("zeta", pid=15)
    calls: list[str] = []

    async def fake_run_turn(system, user, history=None, env=None, *, provider=None, model=None, set_status=None):
        calls.append("call")
        raise ValueError("malformed tool schema")  # not transient, not overflow

    _run(fake_run_turn, to=15)
    assert calls == ["call"]  # single attempt, straight to terminal handler
