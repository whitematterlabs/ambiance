"""First-class `edit` tool: pi semantics — original-content matching,
uniqueness, overlap rejection, BOM/CRLF round-trip, atomic write."""

from __future__ import annotations

from pathlib import Path

import pytest

from boot import edit_tool
from boot import paths as PA


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home" / "pai"
    home.mkdir(parents=True)
    monkeypatch.setattr(PA, "PAI_ROOT", tmp_path, raising=True)
    monkeypatch.setattr(PA, "HOME_DIR", home, raising=True)
    return home


def _edit(path: str, *edits: tuple[str, str], **extra):
    return edit_tool.run({
        "path": path,
        "edits": [{"oldText": o, "newText": n} for o, n in edits],
        **extra,
    })


def test_single_edit(home: Path) -> None:
    f = home / "f.txt"
    f.write_text("hello world")
    r = _edit("f.txt", ("world", "there"))
    assert not r.is_error
    assert r.text == "Successfully replaced 1 block(s) in f.txt."
    assert f.read_text() == "hello there"


def test_multiple_disjoint_edits_match_original(home: Path) -> None:
    """Edit 2's oldText equals text edit 1 *introduces* — matching against the
    original (not incrementally) must still apply both, exactly once each."""
    f = home / "f.txt"
    f.write_text("one two")
    r = _edit("f.txt", ("one", "two-x"), ("two", "three"))
    assert not r.is_error
    assert r.text == "Successfully replaced 2 block(s) in f.txt."
    assert f.read_text() == "two-x three"


def test_overlapping_edits_rejected(home: Path) -> None:
    f = home / "f.txt"
    f.write_text("abc")
    r = _edit("f.txt", ("ab", "X"), ("bc", "Y"))
    assert r.is_error
    assert "edits[0] and edits[1] overlap in f.txt" in r.text
    assert f.read_text() == "abc"  # untouched on failure


def test_not_found(home: Path) -> None:
    f = home / "f.txt"
    f.write_text("hello")
    r = _edit("f.txt", ("nope", "x"))
    assert r.is_error
    assert "Could not find the exact text in f.txt" in r.text
    r2 = _edit("f.txt", ("hello", "hi"), ("nope", "x"))
    assert r2.is_error
    assert "Could not find edits[1] in f.txt" in r2.text
    assert f.read_text() == "hello"


def test_duplicate(home: Path) -> None:
    f = home / "f.txt"
    f.write_text("dup dup")
    r = _edit("f.txt", ("dup", "x"))
    assert r.is_error
    assert "Found 2 occurrences of the text in f.txt" in r.text


def test_empty_old_text(home: Path) -> None:
    f = home / "f.txt"
    f.write_text("hello")
    r = _edit("f.txt", ("", "x"))
    assert r.is_error
    assert "oldText must not be empty in f.txt." in r.text


def test_edits_as_json_string(home: Path) -> None:
    f = home / "f.txt"
    f.write_text("hello world")
    r = edit_tool.run({
        "path": "f.txt",
        "edits": '[{"oldText": "world", "newText": "there"}]',
    })
    assert not r.is_error
    assert f.read_text() == "hello there"


def test_legacy_top_level_old_new(home: Path) -> None:
    f = home / "f.txt"
    f.write_text("hello world")
    r = edit_tool.run({"path": "f.txt", "oldText": "world", "newText": "there"})
    assert not r.is_error
    assert f.read_text() == "hello there"


def test_crlf_round_trip(home: Path) -> None:
    f = home / "f.txt"
    f.write_text("a\r\nb\r\nc", newline="")
    r = _edit("f.txt", ("b", "B"))
    assert not r.is_error
    assert f.read_text(newline="") == "a\r\nB\r\nc"


def test_crlf_old_text_with_lf_matches(home: Path) -> None:
    """oldText spanning lines is LF-normalized, so it matches a CRLF file."""
    f = home / "f.txt"
    f.write_text("a\r\nb\r\nc", newline="")
    r = _edit("f.txt", ("a\nb", "X"))
    assert not r.is_error
    assert f.read_text(newline="") == "X\r\nc"


def test_bom_preserved(home: Path) -> None:
    f = home / "f.txt"
    f.write_text("\ufeffhello")
    r = _edit("f.txt", ("hello", "world"))
    assert not r.is_error
    assert f.read_text() == "\ufeffworld"


def test_no_change_rejected(home: Path) -> None:
    f = home / "f.txt"
    f.write_text("same")
    r = _edit("f.txt", ("same", "same"))
    assert r.is_error
    assert "No changes made to f.txt" in r.text


def test_missing_file(home: Path) -> None:
    r = _edit("nope.txt", ("a", "b"))
    assert r.is_error
    assert "Could not edit file: nope.txt" in r.text
    assert "ENOENT" in r.text


def test_no_stray_tempfiles(home: Path) -> None:
    f = home / "f.txt"
    f.write_text("hello world")
    _edit("f.txt", ("world", "there"))
    assert [p.name for p in home.iterdir()] == ["f.txt"]


def test_invalid_edits_shape(home: Path) -> None:
    (home / "f.txt").write_text("hello")
    r = edit_tool.run({"path": "f.txt", "edits": []})
    assert r.is_error
    r2 = edit_tool.run({"path": "f.txt", "edits": [{"oldText": "x"}]})
    assert r2.is_error
