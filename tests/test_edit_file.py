"""Tests for src/bin/edit_file.py — exact-string atomic edit binary."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from bin import edit_file as ef


def _run(argv: list[str], stdin: str = "", capsys=None, monkeypatch=None) -> tuple[int, str, str]:
    if stdin and monkeypatch is not None:
        import io
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    rc = ef.main(argv)
    out = err = ""
    if capsys is not None:
        cap = capsys.readouterr()
        out, err = cap.out, cap.err
    return rc, out, err


def test_single_replace_in_middle(tmp_path: Path, capsys):
    f = tmp_path / "x.txt"
    f.write_text("alpha beta gamma\n")
    old = tmp_path / "old"; old.write_text("beta")
    new = tmp_path / "new"; new.write_text("BETA")
    rc = ef.main([str(f), "--old-file", str(old), "--new-file", str(new)])
    assert rc == 0
    assert f.read_text() == "alpha BETA gamma\n"
    out = capsys.readouterr().out
    assert "@@" in out  # unified-diff hunk header


def test_missing_file(tmp_path: Path, capsys):
    old = tmp_path / "old"; old.write_text("x")
    new = tmp_path / "new"; new.write_text("y")
    rc = ef.main([str(tmp_path / "nope"), "--old-file", str(old), "--new-file", str(new)])
    assert rc == 2


def test_no_match(tmp_path: Path):
    f = tmp_path / "x"; f.write_text("hello\n")
    old = tmp_path / "old"; old.write_text("xyz")
    new = tmp_path / "new"; new.write_text("abc")
    rc = ef.main([str(f), "--old-file", str(old), "--new-file", str(new)])
    assert rc == 3
    assert f.read_text() == "hello\n"  # original intact


def test_ambiguous_without_replace_all(tmp_path: Path):
    f = tmp_path / "x"; f.write_text("foo foo foo\n")
    old = tmp_path / "old"; old.write_text("foo")
    new = tmp_path / "new"; new.write_text("bar")
    rc = ef.main([str(f), "--old-file", str(old), "--new-file", str(new)])
    assert rc == 4
    assert f.read_text() == "foo foo foo\n"


def test_replace_all(tmp_path: Path):
    f = tmp_path / "x"; f.write_text("foo foo foo\n")
    old = tmp_path / "old"; old.write_text("foo")
    new = tmp_path / "new"; new.write_text("bar")
    rc = ef.main([str(f), "--old-file", str(old), "--new-file", str(new), "--replace-all"])
    assert rc == 0
    assert f.read_text() == "bar bar bar\n"


def test_arg_error_both_modes(tmp_path: Path):
    f = tmp_path / "x"; f.write_text("hi")
    old = tmp_path / "old"; old.write_text("h")
    rc = ef.main([str(f), "--old-file", str(old), "--old-stdin", "--new-file", str(old)])
    assert rc == 5


def test_arg_error_neither_mode(tmp_path: Path):
    f = tmp_path / "x"; f.write_text("hi")
    rc = ef.main([str(f)])
    assert rc == 5


def test_multiline_edit(tmp_path: Path):
    f = tmp_path / "x"
    f.write_text("def foo():\n    return 1\n\ndef bar():\n    return 2\n")
    old = tmp_path / "old"; old.write_text("def foo():\n    return 1\n")
    new = tmp_path / "new"; new.write_text("def foo():\n    return 42\n")
    rc = ef.main([str(f), "--old-file", str(old), "--new-file", str(new)])
    assert rc == 0
    assert f.read_text() == "def foo():\n    return 42\n\ndef bar():\n    return 2\n"


def test_trailing_newline_preserved(tmp_path: Path):
    f = tmp_path / "x"; f.write_text("hello world\n")
    old = tmp_path / "old"; old.write_text("world")
    new = tmp_path / "new"; new.write_text("PAI")
    ef.main([str(f), "--old-file", str(old), "--new-file", str(new)])
    assert f.read_text() == "hello PAI\n"


def test_no_trailing_newline_preserved(tmp_path: Path):
    f = tmp_path / "x"; f.write_text("hello world")
    old = tmp_path / "old"; old.write_text("world")
    new = tmp_path / "new"; new.write_text("PAI")
    ef.main([str(f), "--old-file", str(old), "--new-file", str(new)])
    assert f.read_text() == "hello PAI"


def test_symlink_resolved(tmp_path: Path):
    real = tmp_path / "real.txt"; real.write_text("hello world\n")
    link = tmp_path / "link.txt"
    os.symlink(real, link)
    old = tmp_path / "old"; old.write_text("world")
    new = tmp_path / "new"; new.write_text("PAI")
    rc = ef.main([str(link), "--old-file", str(old), "--new-file", str(new)])
    assert rc == 0
    assert real.read_text() == "hello PAI\n"
    assert link.is_symlink()  # symlink itself untouched


def test_no_tempfile_left_behind_on_success(tmp_path: Path):
    f = tmp_path / "x"; f.write_text("hello\n")
    old = tmp_path / "old"; old.write_text("hello")
    new = tmp_path / "new"; new.write_text("HI")
    ef.main([str(f), "--old-file", str(old), "--new-file", str(new)])
    leftovers = [p for p in tmp_path.iterdir() if ".tmp" in p.name]
    assert leftovers == []


def test_stdin_input(tmp_path: Path, monkeypatch):
    import io
    f = tmp_path / "x"; f.write_text("hello world\n")
    new = tmp_path / "new"; new.write_text("PAI")
    monkeypatch.setattr("sys.stdin", io.StringIO("world"))
    rc = ef.main([str(f), "--old-stdin", "--new-file", str(new)])
    assert rc == 0
    assert f.read_text() == "hello PAI\n"


def test_empty_old_rejected(tmp_path: Path):
    f = tmp_path / "x"; f.write_text("hello\n")
    old = tmp_path / "old"; old.write_text("")
    new = tmp_path / "new"; new.write_text("x")
    rc = ef.main([str(f), "--old-file", str(old), "--new-file", str(new)])
    assert rc == 5
