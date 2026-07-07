"""Runtime build identity + skew/heal policy (boot.build).

Foolproofs the version-skew failure where a new web console runs against an
old (un-rebooted) kernel: each process reports the build its own code was
loaded from, and the web surface auto-heals a stale kernel by rebooting it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from boot import build as B
from boot import paths as PA


# --- ground-truth version from a process's own __file__ --------------------


def test_version_derived_from_opt_pai_path() -> None:
    f = Path("/Users/x/.pai/opt/pai/0.1.0+build.25/src/boot/build.py")
    assert B._version_from_path(f) == "0.1.0+build.25"


def test_version_none_for_dev_checkout_path() -> None:
    f = Path("/Users/x/Projects/pai/src/boot/build.py")
    assert B._version_from_path(f) is None


def test_running_build_reports_dev_outside_opt_pai() -> None:
    # In this repo the test runs from a git checkout (not under opt/pai).
    b = B.running_build()
    assert b.dev is True
    assert b.version == "dev"


# --- kernel stamp round-trip ------------------------------------------------


def test_kernel_stamp_write_then_read(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(PA, "PAI_ROOT", tmp_path, raising=True)
    B.write_kernel_stamp(pid=4242)
    stamp = B.read_kernel_stamp()
    assert stamp is not None
    assert stamp["pid"] == 4242
    assert "version" in stamp and "started" in stamp
    # stored as plain JSON under run/pai/build/
    raw = (tmp_path / "run" / "pai" / "build" / "kernel.json").read_text()
    assert json.loads(raw)["pid"] == 4242


def test_read_kernel_stamp_missing_is_none(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(PA, "PAI_ROOT", tmp_path, raising=True)
    assert B.read_kernel_stamp() is None


# --- stateless skew classification -----------------------------------------


@pytest.mark.parametrize(
    "kernel,console,current,expected",
    [
        (None, "b25", "b25", "unknown"),
        ("b25", "b25", "b25", "in_sync"),
        ("b17", "b25", "b25", "kernel_stale"),  # the bug we hit
        ("b25", "b17", "b25", "console_stale"),
        ("b17", "b18", "b25", "both_stale"),
    ],
)
def test_classify_skew(kernel, console, current, expected) -> None:
    assert B.classify_skew(kernel, console, current) == expected


# --- stateful heal policy ---------------------------------------------------


def test_heal_fresh_kernel_stale_triggers_reboot() -> None:
    st = B.HealState()
    assert B.decide_heal("b17", "b25", "b25", st, now=100.0) == "reboot"


def test_heal_none_within_cooldown_after_attempt() -> None:
    st = B.HealState(last_kernel_ver="b17", last_attempt_monotonic=100.0)
    # same stale kernel, 30s later, cooldown 60s -> keep waiting
    assert B.decide_heal("b17", "b25", "b25", st, now=130.0) == "none"


def test_heal_escalates_when_still_stale_after_cooldown() -> None:
    st = B.HealState(last_kernel_ver="b17", last_attempt_monotonic=100.0)
    assert B.decide_heal("b17", "b25", "b25", st, now=200.0) == "escalate"


def test_heal_console_stale_warns_only() -> None:
    st = B.HealState()
    assert B.decide_heal("b25", "b17", "b25", st, now=100.0) == "warn_console"


def test_heal_in_sync_is_none() -> None:
    st = B.HealState(last_kernel_ver="b17", last_attempt_monotonic=100.0)
    assert B.decide_heal("b25", "b25", "b25", st, now=999.0) == "none"


def test_heal_no_kernel_stamp_is_none() -> None:
    st = B.HealState()
    assert B.decide_heal(None, "b25", "b25", st, now=100.0) == "none"


# --- console self-restart policy ---------------------------------------------


@pytest.mark.parametrize(
    "console,current,dev,already,can_restart,expected",
    [
        # the bug: release console behind the installed release -> re-exec
        ("b17", "b25", False, None, True, True),
        # a later release restarts again even after an earlier attempt
        ("b17", "b25", False, "b18", True, True),
        # one attempt per release: already re-exec'd for b25, still stale -> banner
        ("b17", "b25", False, "b25", True, False),
        # console already current
        ("b25", "b25", False, None, True, False),
        # dev checkout consoles are the developer's business
        ("dev", "b25", True, None, True, False),
        # installed target is a dev checkout: nothing to re-exec into
        ("b17", "dev", False, None, True, False),
        # embedder gave us no way to re-exec
        ("b17", "b25", False, None, False, False),
    ],
)
def test_decide_console_restart(console, current, dev, already, can_restart, expected) -> None:
    assert (
        B.decide_console_restart(
            console, current, dev=dev, already=already, can_restart=can_restart
        )
        is expected
    )
