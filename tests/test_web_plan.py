"""Owner plan edits — `write_plan` is the write half of the plan rail.

The console POSTs the full edited markdown to /api/plan; `write_plan` lands it
in `proc/<slug>/plan.md`. Load-bearing invariants: content round-trips through
`read_plan`, whitespace-only content deletes the file (the owner's `rm`), and
the write is atomic (no bare plan.md.tmp left behind for the watcher to race).

Every edit also nudges the PAI (kind `plan_edit`) so it re-reads the file:
`nudge_plan_edit` debounces a burst of checkbox clicks into one event whose
`cleared` flag comes from the burst's last write, and the kernel routes the
event to `_deliver_message` with a re-read instruction (inject-if-busy comes
free with that path).
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
import yaml

from boot import main as main_mod
from boot import processes as P
from usr.libexec.web.pai_web import actions
from usr.libexec.web.pai_web import hub as H


@pytest.fixture
def proc_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(H, "PROC_DIR", tmp_path, raising=True)
    (tmp_path / "pai").mkdir()
    return tmp_path


def test_write_plan_round_trips(proc_dir: Path) -> None:
    md = "# focus\n\n- [ ] step one\n- [x] step two\n"
    H.write_plan("pai", md)
    assert (proc_dir / "pai" / "plan.md").read_text(encoding="utf-8") == md
    assert H.read_plan("pai") == md


def test_write_plan_overwrites(proc_dir: Path) -> None:
    H.write_plan("pai", "- [ ] a\n")
    H.write_plan("pai", "- [x] a\n")
    assert H.read_plan("pai") == "- [x] a\n"


def test_empty_content_deletes(proc_dir: Path) -> None:
    H.write_plan("pai", "- [ ] a\n")
    H.write_plan("pai", "   \n  ")
    assert not (proc_dir / "pai" / "plan.md").exists()
    assert H.read_plan("pai") == ""


def test_empty_content_on_absent_plan_is_noop(proc_dir: Path) -> None:
    H.write_plan("pai", "")
    assert not (proc_dir / "pai" / "plan.md").exists()


def test_no_tmp_residue(proc_dir: Path) -> None:
    H.write_plan("pai", "- [ ] a\n")
    assert [p.name for p in (proc_dir / "pai").iterdir()] == ["plan.md"]


# --- edit → PAI nudge -------------------------------------------------------


def _wait_for(pred, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.01)
    assert pred()


def test_nudge_plan_edit_coalesces_burst(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[dict] = []
    monkeypatch.setattr(actions, "emit_event", events.append)
    monkeypatch.setattr(actions, "_PLAN_NUDGE_DELAY_S", 0.05)
    actions.nudge_plan_edit(7, cleared=False)
    actions.nudge_plan_edit(7, cleared=True)
    actions.nudge_plan_edit(7, cleared=False)  # last write wins
    _wait_for(lambda: len(events) == 1)
    time.sleep(0.15)  # no trailing second fire
    assert events == [
        {"source": "web", "kind": "plan_edit", "target_pid": 7, "cleared": False}
    ]


def test_nudge_plan_edit_cleared_from_last_write(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[dict] = []
    monkeypatch.setattr(actions, "emit_event", events.append)
    monkeypatch.setattr(actions, "_PLAN_NUDGE_DELAY_S", 0.05)
    actions.nudge_plan_edit(8, cleared=False)
    actions.nudge_plan_edit(8, cleared=True)
    _wait_for(lambda: len(events) == 1)
    assert events[0]["cleared"] is True


def _route(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, event: dict) -> list[dict]:
    captured: list[dict] = []

    def fake_deliver(to: int, reason: str, **kwargs):
        captured.append({"to": to, "reason": reason, **kwargs})

    monkeypatch.setattr(main_mod, "_deliver_message", fake_deliver)
    monkeypatch.setattr(P, "slug_for_pid", lambda pid: "pai")
    event_path = tmp_path / "event.yaml"
    event_path.write_text(yaml.safe_dump(event))
    asyncio.run(main_mod._handle_event_file(event_path, []))
    return captured


def test_kernel_routes_plan_edit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ev = {"source": "web", "kind": "plan_edit", "target_pid": 4, "cleared": False}
    [msg] = _route(tmp_path, monkeypatch, ev)
    assert msg["to"] == 4
    assert msg["reason"] == "owner edited plan"
    assert "/proc/pai/plan.md" in msg["context"]["text"]
    assert "Re-read" in msg["context"]["text"]


def test_kernel_routes_plan_cleared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ev = {"source": "web", "kind": "plan_edit", "target_pid": 4, "cleared": True}
    [msg] = _route(tmp_path, monkeypatch, ev)
    assert "cleared your plan" in msg["context"]["text"]


def test_kernel_drops_plan_edit_for_dead_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[dict] = []
    monkeypatch.setattr(
        main_mod, "_deliver_message", lambda *a, **k: captured.append({})
    )

    def gone(pid: int) -> str:
        raise P.ProcessNotFound(str(pid))

    monkeypatch.setattr(P, "slug_for_pid", gone)
    event_path = tmp_path / "event.yaml"
    event_path.write_text(
        yaml.safe_dump({"source": "web", "kind": "plan_edit", "target_pid": 99})
    )
    asyncio.run(main_mod._handle_event_file(event_path, []))
    assert captured == []
