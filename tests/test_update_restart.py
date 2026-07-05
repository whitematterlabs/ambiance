"""`pai update` makes the running system fully live: after repointing the
release it signals the running kernel to re-exec into the new build (unless
--no-restart). This closes the new-web/old-kernel skew gap."""

from __future__ import annotations

import pytest

from bin import pai


def _patch(monkeypatch, *, running: bool):
    emitted: list[dict] = []
    monkeypatch.setattr(pai, "_kernel_is_running", lambda: running)
    monkeypatch.setattr(
        "boot.processes.emit_event", lambda payload: emitted.append(payload)
    )
    return emitted


def test_restart_signals_kernel_when_running(monkeypatch) -> None:
    emitted = _patch(monkeypatch, running=True)
    pai._restart_kernel_after_update("0.1.0+build.26", no_restart=False)
    assert emitted == [{"kind": "kernel:restart", "source": "update"}]


def test_no_restart_flag_skips_signal(monkeypatch) -> None:
    emitted = _patch(monkeypatch, running=True)
    pai._restart_kernel_after_update("0.1.0+build.26", no_restart=True)
    assert emitted == []


def test_no_signal_when_kernel_not_running(monkeypatch) -> None:
    emitted = _patch(monkeypatch, running=False)
    pai._restart_kernel_after_update("0.1.0+build.26", no_restart=False)
    assert emitted == []
