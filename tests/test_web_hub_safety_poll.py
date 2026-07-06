"""The web hub's proc + me-thread watchers must not depend solely on watchdog
FSEvents.

macOS FSEvents coalesces bursty writes and can drop a directory-level
notification outright. A busy multi-tool turn ends with a burst (narration +
command output to kernel.log, then the final reply to the me/ thread, then the
busy flag cleared); if the me-thread or proc event in that burst is dropped, the
reply never reaches the chat and the status sticks on a stale value until the
owner reconnects. The kernel.log tail already backstops this with a 0.5s poll;
these tests pin the same backstop onto the two directory watchers, plus the
change-gating that keeps the poll from spamming identical rows twice a second.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from boot import nudge as N
from boot import paths as PA
from boot import processes as P
from usr.libexec.web.pai_web import hub as H


@pytest.fixture(autouse=True)
def _reset(live_dir: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(N, "HOME_DIR", PA.HOME_DIR, raising=True)
    monkeypatch.setattr(N, "PROC_DIR", P.PROC_DIR, raising=True)


class _FakeSub:
    """Minimal Subscriber stand-in: records every broadcast dict it receives."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send(self, msg: dict) -> None:
        self.sent.append(msg)


def _of_type(sub: _FakeSub, kind: str) -> list[dict]:
    return [m for m in sub.sent if m.get("type") == kind]


def test_procs_broadcast_is_gated_on_change() -> None:
    # Without gating, the safety-net poll would re-broadcast identical proc rows
    # on every tick. A recompute only emits when the rows actually change.
    hub = H.Hub()
    sub = _FakeSub()
    hub.add(sub)
    P.spawn_pai(pid=2, slug="pai")

    hub._recompute_procs(broadcast=True)  # [] -> one row: a real change
    hub._recompute_procs(broadcast=True)  # identical: must not re-broadcast
    assert len(_of_type(sub, "procs")) == 1

    P.mark_busy("pai", "thinking")
    hub._recompute_procs(broadcast=True)  # busy flip: a real change
    assert len(_of_type(sub, "procs")) == 2


def test_recompute_threads_delivers_a_write_with_no_fs_event() -> None:
    # The guarantee the poll relies on each tick: a reply appended to the me/
    # thread reaches subscribers on the next recompute, whether or not an
    # FSEvents notification ever fired for that write.
    hub = H.Hub()
    sub = _FakeSub()
    hub.add(sub)
    P.spawn_pai(pid=2, slug="pai")
    hub._fleet_pids = [2]
    hub._threads[2] = H.read_thread(2)  # seed the cached snapshot (empty)

    N._append_to_me_thread("pai", "Here's what's going on")

    hub._recompute_threads()
    threads = _of_type(sub, "thread")
    assert len(threads) == 1
    assert threads[0]["pid"] == 2
    assert any("Here's what's going on" in m["body"] for m in threads[0]["messages"])

    # Idempotent: an unchanged thread on the next tick emits nothing new.
    hub._recompute_threads()
    assert len(_of_type(sub, "thread")) == 1


def test_safety_tick_pokes_every_watcher() -> None:
    # The backstop must poke BOTH directory watchers — the me-thread miss lost
    # the reply and the proc miss stuck the status. A tick that poked only one
    # would fix half the bug.
    hub = H.Hub()
    proc_worker = H._Debounced(lambda: None)
    me_worker = H._Debounced(lambda: None)

    hub._safety_tick([proc_worker, me_worker])

    assert proc_worker._ev.is_set()
    assert me_worker._ev.is_set()
