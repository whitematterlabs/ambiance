"""kernel.log tee behavior.

Regression guard for two bugs:
1. The tee used to be gated on `sys.stdout.isatty()`, so headless starts
   (backgrounded shell, the now-removed launchd job) silently logged nothing —
   exactly the always-on case the log exists for.
2. The log path was built from HOME_DIR (= PAI_ROOT/home/<pai>), burying a
   system log inside a PAI's stitched home instead of the FHS-canonical
   PAI_ROOT/var/log/kernel/kernel.log that the app and TUI read.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from boot import main as M
from boot import paths


def _restore_std():
    sys.stdout = sys.__stdout__
    sys.stderr = sys.__stderr__


def test_headless_start_writes_canonical_log(tmp_path: Path, monkeypatch):
    """stdout not a tty and not the log file → tee installs, log gets written."""
    monkeypatch.setattr(paths, "PAI_ROOT", tmp_path, raising=True)
    log = tmp_path / "var" / "log" / "kernel" / "kernel.log"

    saved_out, saved_err = sys.stdout, sys.stderr
    try:
        sys.stdout = open(os.devnull, "w")  # headless: not a tty, not the log
        M._install_stdout_tee()
        print("HEADLESS_LINE")
        sys.stdout.flush()
    finally:
        sys.stdout, sys.stderr = saved_out, saved_err

    assert log.exists(), "headless start must create the canonical kernel.log"
    assert "HEADLESS_LINE" in log.read_text()


def test_caller_owns_log_is_not_double_written(tmp_path: Path, monkeypatch):
    """stdout already IS the log file (PAI.app redirect) → tee skips, no dup."""
    monkeypatch.setattr(paths, "PAI_ROOT", tmp_path, raising=True)
    log = tmp_path / "var" / "log" / "kernel" / "kernel.log"
    log.parent.mkdir(parents=True, exist_ok=True)

    saved_out, saved_err = sys.stdout, sys.stderr
    try:
        sys.stdout = open(log, "a")  # caller pointed our stdout straight at it
        M._install_stdout_tee()
        print("APP_LINE")
        sys.stdout.flush()
    finally:
        sys.stdout, sys.stderr = saved_out, saved_err

    assert log.read_text().count("APP_LINE") == 1, "must not double-write"


def test_path_is_canonical_not_under_home(monkeypatch, tmp_path):
    """The tee writes to PAI_ROOT/var/log, never PAI_ROOT/home/<pai>/var/log."""
    monkeypatch.setattr(paths, "PAI_ROOT", tmp_path, raising=True)
    assert paths.var_log() / "kernel" / "kernel.log" == (
        tmp_path / "var" / "log" / "kernel" / "kernel.log"
    )
