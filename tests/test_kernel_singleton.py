"""Once a kernel holds the flock it is the sole legitimate kernel for its
PAI_ROOT. `_find_duplicate_kernel_pids` identifies *other* live kernels (e.g.
stragglers from a boot that predated the flock) so boot() can SIGTERM them,
enforcing the single-writer invariant on driver state.

Regression: three concurrent kernels once raced on a driver's shared
`cursor.yaml.tmp`, crash-looping the driver with FileNotFoundError.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from boot import entry as E
from boot import paths as PA


@pytest.fixture
def venv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    root = tmp_path / "pai"
    monkeypatch.setattr(PA, "PAI_ROOT", root, raising=True)
    return str(PA.venv_python())


def _ps(*lines: str) -> str:
    return "\n".join(lines) + "\n"


def test_detects_other_kernel_same_root(venv: str) -> None:
    out = _ps(
        f"4242 {venv} -u -m boot.entry",
        f"100 {venv} -u -m boot.entry",  # self
    )
    assert E._find_duplicate_kernel_pids(out, self_pid=100) == [4242]


def test_excludes_self(venv: str) -> None:
    out = _ps(f"100 {venv} -u -m boot.entry")
    assert E._find_duplicate_kernel_pids(out, self_pid=100) == []


def test_matches_bare_and_arg_module_forms(venv: str) -> None:
    out = _ps(
        f"1 {venv} -m boot",          # boot/__main__
        f"2 {venv} -m boot run",      # __main__ with arg
        f"3 {venv} -u -m boot.entry",  # explicit entry
    )
    assert E._find_duplicate_kernel_pids(out, self_pid=999) == [1, 2, 3]


def test_excludes_sibling_modules(venv: str) -> None:
    """The TUI and other boot.* modules are not kernels."""
    out = _ps(
        f"5 {venv} -m boot.tui",
        f"6 {venv} -m boot.something",
    )
    assert E._find_duplicate_kernel_pids(out, self_pid=999) == []


def test_excludes_non_kernel_commands(venv: str) -> None:
    out = _ps(
        f"7 {venv} -m subagent run",
        "8 /usr/bin/grep -m boot.entry",  # not our interpreter
        "9 vim src/boot/entry.py",
    )
    assert E._find_duplicate_kernel_pids(out, self_pid=999) == []


def test_excludes_kernel_of_different_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(PA, "PAI_ROOT", tmp_path / "mine", raising=True)
    other_root_python = str((tmp_path / "other" / "usr" / "lib" / "venv" / "bin" / "python"))
    out = _ps(f"321 {other_root_python} -u -m boot.entry")
    assert E._find_duplicate_kernel_pids(out, self_pid=999) == []


def test_ignores_malformed_lines(venv: str) -> None:
    out = _ps(
        "",
        "not-a-pid here",
        f"-3 {venv} -m boot.entry",  # nonpositive pid
        f"55 {venv} -u -m boot.entry",
    )
    assert E._find_duplicate_kernel_pids(out, self_pid=999) == [55]
