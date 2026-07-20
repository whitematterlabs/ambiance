"""First-class `write` tool: parent creation, overwrite, path resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from boot import paths as PA
from boot import write_tool


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home" / "pai"
    home.mkdir(parents=True)
    monkeypatch.setattr(PA, "PAI_ROOT", tmp_path, raising=True)
    monkeypatch.setattr(PA, "HOME_DIR", home, raising=True)
    return home


def test_creates_nested_parents(home: Path) -> None:
    r = write_tool.run({"path": "a/b/c.txt", "content": "deep"})
    assert not r.is_error
    assert (home / "a" / "b" / "c.txt").read_text() == "deep"


def test_overwrites_existing(home: Path) -> None:
    f = home / "f.txt"
    f.write_text("old")
    r = write_tool.run({"path": "f.txt", "content": "new"})
    assert not r.is_error
    assert f.read_text() == "new"


def test_fhs_illusion_spelling_rejected(home: Path) -> None:
    r = write_tool.run({"path": "/home/pai/out.txt", "content": "x"})
    assert r.is_error
    assert f"{PA.PAI_ROOT}/home/pai/out.txt" in r.text
    assert not (home / "out.txt").exists()


def test_byte_count_message(home: Path) -> None:
    r = write_tool.run({"path": "f.txt", "content": "héllo"})
    assert r.text == "Successfully wrote 6 bytes to f.txt"


def test_missing_args(home: Path) -> None:
    assert write_tool.run({"content": "x"}).is_error
    assert write_tool.run({"path": "f.txt"}).is_error
