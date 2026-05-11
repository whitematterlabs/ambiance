"""Smoke test the full boot path end-to-end against a fresh PAI_ROOT."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest


@pytest.mark.timeout(30)
def test_boot_runs_and_supervises(tmp_path: Path) -> None:
    """sbin/init succeeds in --check-only after paifs_init lays out
    the skeleton, and `python -m boot` reaches the supervise loop."""
    from bin.paifs_init import lay_out
    lay_out(tmp_path)
    env = {**os.environ, "PAI_ROOT": str(tmp_path)}
    # Use the tmp_path's provisioned kernel venv — its .pth file puts
    # usr/lib (and thus the installed drivers) on sys.path, which the
    # repo's dev .venv does not.
    py = str(tmp_path / "usr" / "lib" / "venv" / "bin" / "python")

    # check-only path
    rc = subprocess.run(
        [py, "-m", "boot.init", "--check-only"], env=env
    ).returncode
    assert rc == 0

    # full boot — let it run for a few seconds, then SIGTERM
    proc = subprocess.Popen(
        [py, "-u", "-m", "boot"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    saw_supervise = False
    out_buf: list[str] = []
    try:
        deadline = time.time() + 10
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                if proc.poll() is not None:
                    break
                continue
            out_buf.append(line)
            if "supervise: started" in line:
                saw_supervise = True
                break
    finally:
        # Always reap the subprocess group, even if assertions or the
        # readline loop raised. Escalate SIGTERM → SIGKILL so a hung
        # kernel never leaks into a real ~/.pai run after the pytest
        # tempdir gets reaped.
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(proc.pid, sig)
            except ProcessLookupError:
                break
            try:
                proc.wait(timeout=5)
                break
            except subprocess.TimeoutExpired:
                continue
    assert saw_supervise, f"never reached supervise loop. output:\n{''.join(out_buf)}"
