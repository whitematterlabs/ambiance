"""Per-test isolation: redirect HOME_DIR (and dependent paths) to a tmpdir.

The kernel modules cache `HOME_DIR`, `PROC_DIR`, `EVENTS_DIR` at import
time. This fixture rewrites those module-level globals for the duration
of each test so reconcile and friends operate against a throwaway tree."""

from __future__ import annotations

import os
import pwd
import sys
from pathlib import Path

import pytest

# Make installed drivers importable during tests. The dev .venv's editable
# install only puts `src/` on sys.path; the real drivers live at
# $PAI_ROOT/usr/lib/drivers/<name>/ (installed by paiman). Tests that
# import boot.main need them since boot.main does `from drivers import contacts`.
_real_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
_pai_root = Path(os.environ.get("PAI_ROOT", str(_real_home / ".pai")))
_usr_lib = str(_pai_root / "usr" / "lib")
if _usr_lib not in sys.path:
    sys.path.insert(0, _usr_lib)

from boot import config as C
from boot import paths as PA
from boot import processes as P


@pytest.fixture(autouse=True)
def _isolate_event_queue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Safety net: no test may emit into the *live* kernel's event queue.

    `processes.EVENTS_DIR` is import-cached to the real `$PAI_ROOT/run/pai/
    events/`. A test that monkeypatches only `paths.PAI_ROOT` (e.g. the paiman
    `fhs_root` fixture) leaves `processes.EVENTS_DIR` pointed at `~/.pai`, so
    `paiman install` during the suite drops `kernel:reload_config` files that
    the running kernel consumes — draining nudges and reaping in-flight
    subagents on the developer's live machine.

    Redirect it to a throwaway dir for every test. Autouse fixtures are set up
    before explicitly-requested ones, so fixtures like `live_dir` that set
    `P.EVENTS_DIR` themselves still win where a test needs to inspect emits.
    """
    monkeypatch.setattr(P, "EVENTS_DIR", tmp_path / "events", raising=True)


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
    # Keep the FHS invariant: HOME_DIR lives under PAI_ROOT. Code that maps a
    # transcript back to a namespace-absolute path (nudge._history_path_display)
    # reads paths.PAI_ROOT dynamically; without this it stays the real ~/.pai
    # and `.relative_to()` raises against the tmp home.
    monkeypatch.setattr(PA, "PAI_ROOT", tmp_path, raising=True)
    monkeypatch.setattr(P, "PAI_ROOT", tmp_path, raising=True)
    return live


@pytest.fixture
def repo_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate the config/packages roots inside tmp_path."""
    root = tmp_path / "repo"
    (root / "etc").mkdir(parents=True)
    (root / "packages").mkdir(parents=True)
    monkeypatch.setattr(C, "CONFIG_PATH", root / "etc" / "config.yaml", raising=True)
    monkeypatch.setattr(C, "PACKAGES_DIR", root / "packages", raising=True)
    return root
