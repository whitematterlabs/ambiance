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

    # check-only path
    rc = subprocess.run(
        [sys.executable, "-m", "boot.init", "--check-only"], env=env
    ).returncode
    assert rc == 0

    # full boot — let it run for a few seconds, then SIGTERM
    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "boot"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    deadline = time.time() + 10
    saw_supervise = False
    out_buf: list[str] = []
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
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    proc.wait(timeout=5)
    assert saw_supervise, f"never reached supervise loop. output:\n{''.join(out_buf)}"
