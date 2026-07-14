"""Per-PAI idle heartbeat — duration parsing, heap arming, config plumbing.

The heartbeat is a synthetic `heartbeat:<slug>` entry in the kernel's one
timer heap, re-armed at every turn end and (re)built from spec.yaml at boot
and on spec writes. These tests cover the pure primitives (timers), the
config field's validation/reconcile round-trip, and the setter the web
console drives.

Isolation: monkeypatch the cached PROC_DIR globals (per conftest's
`live_dir` pattern). Never reload modules.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

import pytest
import yaml

from boot import config as C
from boot import main as M
from boot import processes as P
from boot import proc_watcher as PW
from boot import timers as T


# ----- parse_duration -----


@pytest.mark.parametrize(
    ("value", "secs"),
    [
        ("90s", 90),
        ("30m", 1800),
        ("1h", 3600),
        ("2d", 172800),
        ("1H", 3600),  # case-insensitive
        (" 45m ", 2700),  # tolerant of whitespace
        (3600, 3600),  # bare int = seconds
        (90, 90),
    ],
)
def test_parse_duration_valid(value, secs) -> None:
    assert T.parse_duration(value) == secs


@pytest.mark.parametrize(
    "value",
    [
        "1x",  # unknown unit
        "30",  # bare string without unit is ambiguous
        "m",  # unit without amount
        "1.5h",  # fractions not supported
        "-5m",
        "0s",
        0,
        -10,
        True,  # bool is int-shaped junk
        False,
        None,
        [60],
    ],
)
def test_parse_duration_invalid(value) -> None:
    with pytest.raises(ValueError):
        T.parse_duration(value)


# ----- arm_heartbeat -----


def test_arm_heartbeat_idempotent() -> None:
    heap: list[T.TimerEntry] = []
    now = datetime(2026, 7, 14, 12, 0, 0)
    T.arm_heartbeat(heap, "librarian", 3600, now)
    fire = T.arm_heartbeat(heap, "librarian", 1800, now)
    entries = [e for e in heap if e.slug == "heartbeat:librarian"]
    assert len(entries) == 1  # re-arm replaced, never stacked
    assert entries[0].fire_time == fire
    assert (fire - now).total_seconds() == 1800


def test_arm_heartbeat_leaves_other_slugs_alone() -> None:
    heap: list[T.TimerEntry] = []
    now = datetime(2026, 7, 14, 12, 0, 0)
    T.push(heap, now, "librarian")  # a real proc timer for the same PAI
    T.arm_heartbeat(heap, "librarian", 600, now)
    T.arm_heartbeat(heap, "other", 600, now)
    assert {e.slug for e in heap} == {
        "librarian",
        "heartbeat:librarian",
        "heartbeat:other",
    }


# ----- rebuild_from_proc / proc_watcher arming -----


@pytest.fixture()
def proc_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    proc = tmp_path / "home" / "proc"
    proc.mkdir(parents=True)
    monkeypatch.setattr(P, "PROC_DIR", proc, raising=True)
    # timers.py binds PROC_DIR by value at import; redirect its copy too.
    monkeypatch.setattr(T, "PROC_DIR", proc, raising=True)
    return proc


def test_rebuild_arms_heartbeat_for_running_pai(proc_dir) -> None:
    P.spawn("librarian", {"kind": "pai", "pid": 3, "heartbeat": "1h"})
    heap = T.rebuild_from_proc()
    assert any(e.slug == "heartbeat:librarian" for e in heap)


def test_rebuild_skips_stopped_and_junk(proc_dir) -> None:
    P.spawn("stopped", {"kind": "pai", "pid": 3, "heartbeat": "1h"})
    P.resolve("stopped", "stopped")
    # A hand-edited junk value must not break boot — swallowed, not armed.
    P.spawn("junk", {"kind": "pai", "pid": 4, "heartbeat": "soon"})
    P.spawn("plain", {"kind": "pai", "pid": 5})
    heap = T.rebuild_from_proc()
    assert not any(e.slug.startswith(T.HEARTBEAT_PREFIX) for e in heap)


def test_schedule_spec_arms_and_cancels_heartbeat(proc_dir) -> None:
    """The proc watcher path: a spec write re-arms from the fresh spec; a
    spec without the field (config removal via reconcile) cancels the beat."""
    P.spawn("librarian", {"kind": "pai", "pid": 3, "heartbeat": "30m"})
    heap: list[T.TimerEntry] = []
    PW._schedule_spec(heap, "librarian")
    assert any(e.slug == "heartbeat:librarian" for e in heap)

    spec = P.read_spec("librarian")
    del spec["heartbeat"]
    with (proc_dir / "librarian" / "spec.yaml").open("w") as f:
        yaml.safe_dump(spec, f, sort_keys=False)
    PW._schedule_spec(heap, "librarian")
    assert not any(e.slug == "heartbeat:librarian" for e in heap)


# ----- the kernel fire → turn → re-arm cycle -----


def test_fire_heartbeat_nudges_idle_pai_and_turn_end_rearms(
    live_dir, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The invariant end to end: a fired beat starts a turn; the turn's
    `finally` re-arms exactly one fresh heap entry (the fire path itself
    never re-arms)."""
    P.spawn_pai(pid=3, slug="lib", description="x", heartbeat="5m")
    heap: list[T.TimerEntry] = []
    monkeypatch.setattr(M, "_timer_heap", heap, raising=True)

    calls: list[dict] = []

    async def fake_nudge(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})

    monkeypatch.setattr(M, "nudge", fake_nudge, raising=True)

    async def drive():
        M._fire_heartbeat("lib")
        [task] = M._active_nudges[3]
        await task

    asyncio.run(drive())

    [call] = calls
    assert call["args"][0] == "heartbeat"
    assert call["kwargs"]["to"] == 3
    entries = [e for e in heap if e.slug == "heartbeat:lib"]
    assert len(entries) == 1  # re-armed by the turn's finally, exactly once


def test_fire_heartbeat_skips_busy_and_stale(
    live_dir, monkeypatch: pytest.MonkeyPatch
) -> None:
    heap: list[T.TimerEntry] = []
    monkeypatch.setattr(M, "_timer_heap", heap, raising=True)
    dispatched: list[int] = []
    monkeypatch.setattr(
        M, "_dispatch_nudge", lambda pid, *a, **k: dispatched.append(pid),
        raising=True,
    )

    # Busy PAI: in-flight turn's own finally re-arms — drop silently.
    P.spawn_pai(pid=3, slug="busy-pai", description="x", heartbeat="5m")
    P.mark_busy("busy-pai", "working")
    M._fire_heartbeat("busy-pai")

    # Config cleared since arming: stale beat dies here.
    P.spawn_pai(pid=4, slug="no-hb", description="x")
    M._fire_heartbeat("no-hb")

    # Proc gone entirely.
    M._fire_heartbeat("ghost")

    assert dispatched == []
    assert heap == []  # and none of the drops re-armed anything


# ----- config validation -----


_HB_CONFIG = """
pais:
  - name: root
    pid: 1
    description: km
  - name: pai
    pid: 2
    description: dflt
    heartbeat: {value}
"""


def _write_config(repo_root: Path, body: str) -> Path:
    path = repo_root / "etc" / "config.yaml"
    path.write_text(body)
    return path


@pytest.mark.parametrize("value", ["1h", "'90s'", "3600"])
def test_config_heartbeat_valid(repo_root, value) -> None:
    _write_config(repo_root, _HB_CONFIG.format(value=value))
    cfg = C.load_config()
    assert "heartbeat" in cfg["pai"]


@pytest.mark.parametrize("value", ["1x", "'30'", "true", "30s"])
def test_config_heartbeat_invalid(repo_root, value) -> None:
    # "30s" parses but is under the 60s floor — an LLM-spend footgun.
    _write_config(repo_root, _HB_CONFIG.format(value=value))
    with pytest.raises(C.ConfigError, match="heartbeat"):
        C.load_config()


def test_config_rejects_colon_in_name(repo_root) -> None:
    # ":" is reserved for synthetic heap slugs (heartbeat:<pai>).
    _write_config(
        repo_root,
        """
pais:
  - name: root
    pid: 1
    description: km
  - name: 'heartbeat:x'
    description: sneaky
""",
    )
    with pytest.raises(C.ConfigError, match="invalid name"):
        C.load_config()


# ----- reconcile round-trip -----


def test_reconcile_heartbeat_add_update_remove(repo_root, live_dir) -> None:
    base = """
pais:
  - name: root
    pid: 1
    description: km
  - name: pai
    pid: 2
    description: dflt
{extra}
"""
    _write_config(repo_root, base.format(extra="    heartbeat: 1h"))
    C.reconcile_from_config()
    assert P.read_spec("pai")["heartbeat"] == "1h"

    _write_config(repo_root, base.format(extra="    heartbeat: 30m"))
    C.reconcile_from_config()
    assert P.read_spec("pai")["heartbeat"] == "30m"

    _write_config(repo_root, base.format(extra=""))
    C.reconcile_from_config()
    assert "heartbeat" not in P.read_spec("pai")


# ----- set_pai_heartbeat (the console write path) -----


def test_set_pai_heartbeat_set_and_clear(repo_root) -> None:
    path = _write_config(
        repo_root,
        """
pais:
  - name: pai
    pid: 2
    description: dflt
""",
    )
    out = C.set_pai_heartbeat("pai", "45m")
    assert out == {"name": "pai", "heartbeat": "45m"}
    raw = yaml.safe_load(path.read_text())
    assert raw["pais"][0]["heartbeat"] == "45m"

    # None (and blank) removes the key — heartbeat off.
    out = C.set_pai_heartbeat("pai", None)
    assert out["heartbeat"] is None
    raw = yaml.safe_load(path.read_text())
    assert "heartbeat" not in raw["pais"][0]


def test_set_pai_heartbeat_rejects_junk(repo_root) -> None:
    path = _write_config(
        repo_root,
        """
pais:
  - name: pai
    pid: 2
    description: dflt
""",
    )
    before = path.read_text()
    with pytest.raises(ValueError):
        C.set_pai_heartbeat("pai", "1x")
    with pytest.raises(ValueError, match="60s"):
        C.set_pai_heartbeat("pai", "30s")
    with pytest.raises(ValueError, match="unknown pai"):
        C.set_pai_heartbeat("ghost", "1h")
    assert path.read_text() == before  # file untouched on every failure
