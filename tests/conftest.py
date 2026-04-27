"""Per-test isolation: redirect HOME_DIR (and dependent paths) to a tmpdir.

The kernel modules cache `HOME_DIR`, `PROC_DIR`, `EVENTS_DIR` at import
time. This fixture rewrites those module-level globals for the duration
of each test so reconcile and friends operate against a throwaway tree."""

from __future__ import annotations

from pathlib import Path

import pytest

from kernel import config as C
from kernel import processes as P


@pytest.fixture
def live_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    live = tmp_path / "home"
    proc = live / "proc"
    events = live / "events"
    proc.mkdir(parents=True)
    events.mkdir(parents=True)
    monkeypatch.setattr(P, "HOME_DIR", live, raising=True)
    monkeypatch.setattr(P, "PROC_DIR", proc, raising=True)
    monkeypatch.setattr(P, "EVENTS_DIR", events, raising=True)
    return live


@pytest.fixture
def repo_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate the config/packages roots inside tmp_path."""
    root = tmp_path / "repo"
    (root / "etc").mkdir(parents=True)
    (root / "packages").mkdir(parents=True)
    monkeypatch.setattr(C, "REPO_ROOT", root, raising=True)
    monkeypatch.setattr(C, "CONFIG_PATH", root / "etc" / "config.yaml", raising=True)
    monkeypatch.setattr(C, "PACKAGES_DIR", root / "packages", raising=True)
    return root
