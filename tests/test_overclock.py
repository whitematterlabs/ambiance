from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path

import pytest
import yaml

from boot import main as M
from boot import nudge as N
from boot import processes as P


@pytest.fixture(autouse=True)
def _reset(live_dir: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(N, "_pai_locks", {}, raising=True)
    monkeypatch.setattr(N, "_recently_compacted", {}, raising=True)
    monkeypatch.setattr(N, "_TRANSIENT_RETRY_DELAY", 0, raising=True)
    monkeypatch.setattr(N, "HOME_DIR", P.HOME_DIR, raising=True)
    monkeypatch.setattr(N, "PROC_DIR", P.PROC_DIR, raising=True)


def _spawn(slug: str, *, pid: int, **extra) -> None:
    P.spawn_pai(pid=pid, slug=slug, description=f"{slug} test", extra=extra or None)


def _me_thread(slug: str) -> str:
    path = (
        P.HOME_DIR
        / "communication"
        / "messages"
        / "me"
        / slug
        / f"{date.today().isoformat()}.md"
    )
    return path.read_text() if path.exists() else ""


def _patch_run_turn(fake):
    import boot.llm as L

    orig = L.run_turn
    L.run_turn = fake  # type: ignore[assignment]
    return L, orig


def test_overclock_repeats_until_sentinel_and_strips_visible_reply() -> None:
    _spawn("clock", pid=50)
    replies = ["still checking", f"found it {N.OVERCLOCK_SENTINEL}"]
    users: list[str] = []
    busy_reasons: list[str] = []

    async def fake_run_turn(system, user, history=None, env=None, *, provider=None, model=None, set_status=None):
        users.append(user)
        if set_status:
            set_status("waiting on fake")
        busy = P.read_busy("clock")
        if busy:
            busy_reasons.append(busy[0])
        reply = replies[len(users) - 1]
        return (reply, list(history or []) + [
            {"role": "user", "content": user},
            {"role": "assistant", "content": reply},
        ])

    L, orig = _patch_run_turn(fake_run_turn)
    try:
        asyncio.run(
            N.nudge(
                reason="owner message",
                to=50,
                from_kind="kernel",
                context={"thread": "me", "sender": "me", "text": "find a hotel", "overclock": True},
            )
        )
    finally:
        L.run_turn = orig  # type: ignore[assignment]

    assert len(users) == 2
    assert "Overclock mode is active" in users[0]
    assert "overclock continue" in users[1]
    assert busy_reasons[0].startswith("overclock: turn 1")
    assert busy_reasons[1].startswith("overclock: turn 2")
    thread = _me_thread("clock")
    assert "still checking" in thread
    assert "found it" in thread
    assert N.OVERCLOCK_SENTINEL not in thread


def test_overclock_runs_unbounded_until_sentinel() -> None:
    # No turn cap: the loop must keep going well past the old 10-turn limit
    # and stop only on the sentinel.
    _spawn("limit", pid=51)
    calls = 0

    async def fake_run_turn(system, user, history=None, env=None, *, provider=None, model=None, set_status=None):
        nonlocal calls
        calls += 1
        reply = "not done yet" if calls < 25 else f"done {N.OVERCLOCK_SENTINEL}"
        return (reply, list(history or []) + [
            {"role": "user", "content": user},
            {"role": "assistant", "content": reply},
        ])

    L, orig = _patch_run_turn(fake_run_turn)
    try:
        asyncio.run(
            N.nudge(
                reason="owner message",
                to=51,
                from_kind="kernel",
                context={"text": "keep going", "overclock": True},
            )
        )
    finally:
        L.run_turn = orig  # type: ignore[assignment]

    assert calls == 25
    thread = _me_thread("limit")
    assert "Overclock stopped" not in thread
    assert N.OVERCLOCK_SENTINEL not in thread


def test_overclock_cancellation_does_not_continue() -> None:
    _spawn("cancel", pid=52)
    calls = 0

    async def fake_run_turn(system, user, history=None, env=None, *, provider=None, model=None, set_status=None):
        nonlocal calls
        calls += 1
        import boot.llm as L

        raise L.TurnCancelled(list(history or []) + [{"role": "user", "content": user}])

    L, orig = _patch_run_turn(fake_run_turn)
    try:
        asyncio.run(
            N.nudge(
                reason="owner message",
                to=52,
                from_kind="kernel",
                context={"text": "keep going", "overclock": True},
            )
        )
    finally:
        L.run_turn = orig  # type: ignore[assignment]

    assert calls == 1
    assert _me_thread("cancel") == ""


def test_owner_message_event_routes_overclock_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict] = []
    event_path = tmp_path / "event.yaml"
    event_path.write_text(
        yaml.safe_dump(
            {
                "source": "web",
                "kind": "new_message",
                "thread": "me",
                "target_pid": 53,
                "text": "find a hotel",
                "overclock": True,
            },
            sort_keys=False,
        )
    )

    def fake_dispatch(to, reason, *args, **kwargs):
        captured.append({"to": to, "reason": reason, **kwargs})

    monkeypatch.setattr(M, "_dispatch_nudge", fake_dispatch, raising=True)

    asyncio.run(M._handle_event_file(event_path, []))

    assert captured == [
        {
            "to": 53,
            "reason": "owner message",
            "context": {
                "thread": "me",
                "sender": "me",
                "text": "find a hotel",
                "day_file": f"communication/messages/me/53/{date.today().isoformat()}.md",
                "overclock": True,
            },
        }
    ]
