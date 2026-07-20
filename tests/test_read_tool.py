"""First-class `read` tool: truncation footers, offsets, images, path rules."""

from __future__ import annotations

from pathlib import Path

import pytest

from boot import paths as PA
from boot import read_tool


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated PAI_ROOT with a default home under it."""
    home = tmp_path / "home" / "pai"
    home.mkdir(parents=True)
    monkeypatch.setattr(PA, "PAI_ROOT", tmp_path, raising=True)
    monkeypatch.setattr(PA, "HOME_DIR", home, raising=True)
    return home


def test_basic_read(home: Path) -> None:
    (home / "f.txt").write_text("alpha\nbeta\ngamma")
    r = read_tool.run({"path": "f.txt"})
    assert not r.is_error
    assert r.text == "alpha\nbeta\ngamma"


def test_offset_and_limit(home: Path) -> None:
    (home / "f.txt").write_text("\n".join(f"l{i}" for i in range(1, 11)))
    r = read_tool.run({"path": "f.txt", "offset": 3, "limit": 4})
    assert not r.is_error
    assert r.text.startswith("l3\nl4\nl5\nl6")
    assert "[4 more lines in file. Use offset=7 to continue.]" in r.text


def test_line_limit_footer_and_continuation(home: Path) -> None:
    (home / "big.txt").write_text("\n".join(f"l{i}" for i in range(1, 3001)))
    r = read_tool.run({"path": "big.txt"})
    assert not r.is_error
    assert r.text.startswith("l1\n")
    assert "[Showing lines 1-2000 of 3000. Use offset=2001 to continue.]" in r.text
    # Continue where the footer said.
    r2 = read_tool.run({"path": "big.txt", "offset": 2001})
    assert not r2.is_error
    assert r2.text.startswith("l2001\n")
    assert r2.text.endswith("l3000")
    assert "[Showing" not in r2.text


def test_byte_limit_footer(home: Path) -> None:
    line = "z" * 100
    (home / "wide.txt").write_text("\n".join(line for _ in range(1000)))
    r = read_tool.run({"path": "wide.txt"})
    assert "(50.0KB limit). Use offset=" in r.text


def test_giant_first_line_bash_fallback(home: Path) -> None:
    (home / "one.txt").write_text("q" * 200_000)
    r = read_tool.run({"path": "one.txt"})
    assert not r.is_error
    assert r.text.startswith("[Line 1 is 195.3KB, exceeds 50.0KB limit. Use bash: sed -n '1p' one.txt | head -c 51200]")


def test_offset_past_eof(home: Path) -> None:
    (home / "f.txt").write_text("a\nb")
    r = read_tool.run({"path": "f.txt", "offset": 50})
    assert r.is_error
    assert "Offset 50 is beyond end of file (2 lines total)" in r.text


def test_missing_file_is_error(home: Path) -> None:
    r = read_tool.run({"path": "nope.txt"})
    assert r.is_error
    assert "Could not read file: nope.txt" in r.text
    assert "ENOENT" in r.text


def test_image_returns_markdown_ref(home: Path) -> None:
    img = home / "shot.png"
    img.write_bytes(b"\x89PNG fake")
    r = read_tool.run({"path": "shot.png"})
    assert not r.is_error
    assert r.text == f"Read image file [image/png]\n![]({img})"


def test_fhs_illusion_spelling_rejected(home: Path) -> None:
    root = PA.PAI_ROOT
    (root / "tmp").mkdir()
    (root / "tmp" / "spill.log").write_text("spilled")
    r = read_tool.run({"path": "/tmp/spill.log"})
    assert r.is_error
    assert f"{root}/tmp/spill.log" in r.text


def test_tilde_resolves_to_home(home: Path) -> None:
    (home / "notes.md").write_text("hi")
    r = read_tool.run({"path": "~/notes.md"})
    assert not r.is_error
    assert r.text == "hi"


def test_missing_path_arg(home: Path) -> None:
    r = read_tool.run({})
    assert r.is_error
