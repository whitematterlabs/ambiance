"""The web hub detects kernel-vs-console build skew and auto-reboots a stale
kernel (guarded by cooldown), warns-only when the console itself is stale."""

from __future__ import annotations

import pytest

from boot import build as B
from usr.libexec.web.pai_web import actions
from usr.libexec.web.pai_web import hub as H


def _hub(monkeypatch, *, console, kernel, current):
    h = H.Hub()
    h._console_build = console
    monkeypatch.setattr(B, "read_kernel_stamp", lambda: ({"version": kernel} if kernel else None))
    monkeypatch.setattr(B, "current_release", lambda: current)
    monkeypatch.setattr(h, "_schedule_build_recheck", lambda: None)  # no real timers
    return h


def test_stale_kernel_is_rebooted_once_then_cooled_down(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(actions, "reboot_kernel", lambda: calls.append(1) or {"running": True})
    h = _hub(monkeypatch, console="b25", kernel="b17", current="b25")

    h._recompute_build(broadcast=False)
    assert calls == [1]
    assert h._build_status["state"] == "kernel_stale"

    # Still stale on the next pass, but within cooldown → no second reboot.
    h._recompute_build(broadcast=False)
    assert calls == [1]


def test_in_sync_does_not_reboot(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(actions, "reboot_kernel", lambda: calls.append(1))
    h = _hub(monkeypatch, console="b25", kernel="b25", current="b25")
    h._recompute_build(broadcast=False)
    assert calls == []
    assert h._build_status["state"] == "in_sync"


def test_stale_console_warns_only(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(actions, "reboot_kernel", lambda: calls.append(1))
    h = _hub(monkeypatch, console="b17", kernel="b25", current="b25")
    h._recompute_build(broadcast=False)
    assert calls == []
    assert h._build_status["state"] == "console_stale"


def test_snapshot_includes_build_status(monkeypatch) -> None:
    monkeypatch.setattr(actions, "reboot_kernel", lambda: {"running": True})
    h = _hub(monkeypatch, console="b25", kernel="b25", current="b25")
    h._recompute_build(broadcast=False)
    snap = h.snapshot(provider="anthropic")
    assert snap["build"]["state"] == "in_sync"
    assert snap["build"]["kernel"] == "b25"
