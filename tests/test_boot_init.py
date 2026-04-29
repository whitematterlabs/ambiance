"""/sbin/init: layout-check then exec into kernel."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


def _env(tmp_path: Path) -> dict[str, str]:
    """Build a subprocess env that overrides PAI_ROOT but keeps the
    venv/PYTHONPATH intact so `python -m boot.init` can be found."""
    env = dict(os.environ)
    env["PAI_ROOT"] = str(tmp_path)
    return env


def test_init_fails_loudly_on_missing_layout(tmp_path: Path) -> None:
    """Init bails if PAI_ROOT lacks required dirs."""
    result = subprocess.run(
        [sys.executable, "-m", "boot.init", "--check-only"],
        env=_env(tmp_path),
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "missing" in result.stderr.lower() or "not found" in result.stderr.lower()


def test_init_check_only_passes_on_complete_layout(tmp_path: Path) -> None:
    """Init returns 0 in --check-only mode when layout is valid."""
    from bin.paifs_init import lay_out
    lay_out(tmp_path)
    result = subprocess.run(
        [sys.executable, "-m", "boot.init", "--check-only"],
        env=_env(tmp_path),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
