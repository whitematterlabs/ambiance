"""The kernel pins its cwd to $PAI_ROOT so a swapped-out release dir can't
make os.getcwd()/cwd-less subprocesses raise a fatal bare FileNotFoundError
mid-turn (the cause of subagents being reaped before they finished)."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from boot import entry as E
from boot import paths as PA


def test_ensure_stable_cwd_chdirs_to_pai_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "pai"
    release = root / "opt" / "pai" / "0.1.0"
    release.mkdir(parents=True)
    monkeypatch.setattr(PA, "PAI_ROOT", root, raising=True)

    # Simulate the launch state: cwd is the release dir.
    orig = Path.cwd()
    try:
        os.chdir(release)
        E._ensure_stable_cwd()
        assert Path.cwd() == root.resolve()
    finally:
        os.chdir(orig)


def test_ensure_stable_cwd_survives_deleted_launch_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`pai update` deletes the release dir under the running process. After
    that, getcwd() raises — but a fresh boot() re-pins to the stable root."""
    root = tmp_path / "pai"
    release = root / "opt" / "pai" / "0.1.0"
    release.mkdir(parents=True)
    monkeypatch.setattr(PA, "PAI_ROOT", root, raising=True)

    orig = Path.cwd()
    try:
        os.chdir(release)
        # The update wipes the dir we're sitting in.
        release.rmdir()
        with pytest.raises(FileNotFoundError):
            os.getcwd()
        # Re-pinning recovers without touching the dead inode.
        E._ensure_stable_cwd()
        assert Path.cwd() == root.resolve()
        os.getcwd()  # no longer raises
    finally:
        os.chdir(orig)
