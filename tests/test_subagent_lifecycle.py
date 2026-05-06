"""Subagent lifecycle: persistent spawn, two-channel messaging, explicit done.

Subagents differ from one-shot ephemerals in two ways:
  1. Their spawn spec carries `persistent: true`, so nudge.py does NOT
     auto-resolve them after the initial-prompt turn.
  2. Termination is explicit: the parent calls `bin/subagent done --slug X`,
     which resolves the child and (via the standard proc_resolved path)
     emits an event whose `parent` field routes a final nudge back.

Two messaging kinds are tested:
  - parent→child rides generic `pai_message` (same as any peer IPC). The
    spawn kickoff is just the parent's first such IPC.
  - child→parent uses `subagent:response` (via `bin/subagent reply`), so
    the parent can recognize "this is from one of my own subagents" at a
    glance.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from bin import nudge as nudge_bin
from bin import subagent as sub_bin
from boot import processes as P


def _events(events_dir: Path) -> list[dict]:
    out = []
    for p in sorted(events_dir.iterdir()):
        with p.open() as f:
            out.append(yaml.safe_load(f) or {})
    return out


def test_spawn_marks_subagent_persistent(live_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Parent PAI is pid 1. Subagent must be spawned from a PAI turn.
    P.spawn_pai(pid=1, slug="root", description="parent")
    monkeypatch.setenv("PAI_PID", "1")

    rc = sub_bin.main(["spawn", "--slug", "scratch", "--prompt", "do a thing"])
    assert rc == 0

    # Child proc exists with persistent + parent set.
    [child_slug] = [s for s in P.list_procs() if s.startswith("scratch-")]
    spec = P.read_spec(child_slug)
    assert spec["kind"] == "pai"
    assert spec["parent"] == 1
    assert spec["persistent"] is True
    assert isinstance(spec["pid"], int) and spec["pid"] != 1

    # Kickoff is just a generic pai_message — no special kind.
    [kickoff] = _events(P.EVENTS_DIR)
    assert kickoff["kind"] == "pai_message"
    assert kickoff["target_pid"] == spec["pid"]
    assert kickoff["sender_pid"] == 1
    assert kickoff["text"] == "do a thing"


def test_two_channel_round_trip(live_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Parent → child uses generic pai_message; child → parent uses subagent:response.
    P.spawn_pai(pid=1, slug="root", description="parent")
    monkeypatch.setenv("PAI_PID", "1")
    sub_bin.main(["spawn", "--slug", "scratch", "--prompt", "hi"])
    [child_slug] = [s for s in P.list_procs() if s.startswith("scratch-")]
    child_pid = P.read_spec(child_slug)["pid"]

    for e in P.EVENTS_DIR.iterdir():
        e.unlink()

    # Parent → child via generic nudge.
    monkeypatch.setenv("PAI_PID", "1")
    assert nudge_bin.main(["--to", str(child_pid), "--content", "follow-up question"]) == 0

    # Child → parent via subagent reply (reads $PAI_PARENT).
    monkeypatch.setenv("PAI_PID", str(child_pid))
    monkeypatch.setenv("PAI_PARENT", "1")
    assert sub_bin.main(["reply", "--content", "here is my answer"]) == 0

    events = _events(P.EVENTS_DIR)
    assert len(events) == 2
    p2c, c2p = events
    assert p2c["kind"] == "pai_message" and p2c["target_pid"] == child_pid and p2c["sender_pid"] == 1
    assert p2c["text"] == "follow-up question"
    assert c2p["kind"] == "subagent:response" and c2p["target_pid"] == 1 and c2p["sender_pid"] == child_pid
    assert c2p["text"] == "here is my answer"


def test_reply_requires_parent_env(live_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Calling `subagent reply` without $PAI_PARENT (i.e., from a top-level PAI
    # that has no parent) is an error — only subagents can reply.
    monkeypatch.setenv("PAI_PID", "1")
    monkeypatch.delenv("PAI_PARENT", raising=False)
    rc = sub_bin.main(["reply", "--content", "no one to reply to"])
    assert rc == 1
    assert not list(P.EVENTS_DIR.iterdir())


def test_done_resolves_child_and_emits_proc_resolved(
    live_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    P.spawn_pai(pid=1, slug="root", description="parent")
    monkeypatch.setenv("PAI_PID", "1")
    sub_bin.main(["spawn", "--slug", "scratch", "--prompt", "go"])
    [child_slug] = [s for s in P.list_procs() if s.startswith("scratch-")]

    # Drop the kickoff event so we can read the resolve event clean.
    for e in P.EVENTS_DIR.iterdir():
        e.unlink()

    rc = sub_bin.main(["done", "--slug", child_slug])
    assert rc == 0
    assert P.read_status(child_slug) == "completed"

    [resolved] = _events(P.EVENTS_DIR)
    assert resolved["kind"] == "proc_resolved"
    assert resolved["slug"] == child_slug
    assert resolved["status"] == "completed"
    assert resolved["parent"] == 1


def test_done_refuses_non_owned_subagent(
    live_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # pid 1 spawns a subagent; pid 99 (some other PAI) tries to kill it.
    P.spawn_pai(pid=1, slug="root", description="parent")
    monkeypatch.setenv("PAI_PID", "1")
    sub_bin.main(["spawn", "--slug", "scratch", "--prompt", "go"])
    [child_slug] = [s for s in P.list_procs() if s.startswith("scratch-")]

    monkeypatch.setenv("PAI_PID", "99")
    rc = sub_bin.main(["done", "--slug", child_slug])
    assert rc == 1
    assert P.read_status(child_slug) == "running"
