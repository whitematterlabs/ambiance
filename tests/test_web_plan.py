"""Owner plan edits — `write_plan` is the write half of the plan rail.

The console POSTs the full edited markdown to /api/plan; `write_plan` lands it
in `proc/<slug>/plan.md`. Load-bearing invariants: content round-trips through
`read_plan`, whitespace-only content deletes the file (the owner's `rm`), and
the write is atomic (no bare plan.md.tmp left behind for the watcher to race).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from usr.libexec.web.pai_web import hub as H


@pytest.fixture
def proc_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(H, "PROC_DIR", tmp_path, raising=True)
    (tmp_path / "pai").mkdir()
    return tmp_path


def test_write_plan_round_trips(proc_dir: Path) -> None:
    md = "# focus\n\n- [ ] step one\n- [x] step two\n"
    H.write_plan("pai", md)
    assert (proc_dir / "pai" / "plan.md").read_text(encoding="utf-8") == md
    assert H.read_plan("pai") == md


def test_write_plan_overwrites(proc_dir: Path) -> None:
    H.write_plan("pai", "- [ ] a\n")
    H.write_plan("pai", "- [x] a\n")
    assert H.read_plan("pai") == "- [x] a\n"


def test_empty_content_deletes(proc_dir: Path) -> None:
    H.write_plan("pai", "- [ ] a\n")
    H.write_plan("pai", "   \n  ")
    assert not (proc_dir / "pai" / "plan.md").exists()
    assert H.read_plan("pai") == ""


def test_empty_content_on_absent_plan_is_noop(proc_dir: Path) -> None:
    H.write_plan("pai", "")
    assert not (proc_dir / "pai" / "plan.md").exists()


def test_no_tmp_residue(proc_dir: Path) -> None:
    H.write_plan("pai", "- [ ] a\n")
    assert [p.name for p in (proc_dir / "pai").iterdir()] == ["plan.md"]
