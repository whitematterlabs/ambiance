"""`pai update` makes the running system fully live: after repointing the
release it signals the running kernel to re-exec into the new build (unless
--no-restart), and the `pai start` parent re-execs itself when the hub flags
the console as the stale side. This closes both halves of the build-skew gap."""

from __future__ import annotations

import os
import sys

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


# --- console self re-exec (stale web surface after a release swap) -----------


def test_console_reexec_argv_uses_stable_module_path() -> None:
    argv = pai._console_reexec_argv(9000)
    # exec through the live interpreter + module form: both resolve via paths
    # paifs-init refreshes in place, so the fresh image loads the new release.
    assert argv[:4] == [sys.executable, "-m", "bin.pai", "start"]
    assert argv[4:] == ["--port", "9000", "--no-open"]


def test_pid_if_alive_accepts_our_own_pid() -> None:
    assert pai._pid_if_alive(str(os.getpid())) == os.getpid()


def test_pid_if_alive_rejects_garbage_and_dead_pids() -> None:
    assert pai._pid_if_alive(None) is None
    assert pai._pid_if_alive("") is None
    assert pai._pid_if_alive("not-a-pid") is None
    assert pai._pid_if_alive("-99999999") is None
