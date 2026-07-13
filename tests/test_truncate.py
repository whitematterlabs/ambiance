"""Pi-style tool-output truncation: 2000 lines / 50KB, tail-spill to /tmp."""

from __future__ import annotations

from pathlib import Path

import pytest

from boot import paths as PA
from boot import truncate as T


# ---------------------------------------------------------------- passthrough

def test_head_passthrough_small() -> None:
    t = T.truncate_head("a\nb\nc")
    assert not t.truncated
    assert t.content == "a\nb\nc"
    assert t.total_lines == 3
    assert t.output_lines == 3


def test_tail_passthrough_small() -> None:
    t = T.truncate_tail("a\nb\nc\n")
    assert not t.truncated
    assert t.content == "a\nb\nc\n"
    assert t.total_lines == 3  # trailing newline is not an extra line


# ------------------------------------------------------------------ line limit

def test_tail_line_limit_keeps_last_lines() -> None:
    text = "\n".join(f"line{i}" for i in range(1, 3001))
    t = T.truncate_tail(text)
    assert t.truncated and t.truncated_by == "lines"
    assert t.total_lines == 3000
    assert t.output_lines == T.DEFAULT_MAX_LINES
    assert t.content.startswith("line1001\n")
    assert t.content.endswith("line3000")


def test_head_line_limit_keeps_first_lines() -> None:
    text = "\n".join(f"line{i}" for i in range(1, 3001))
    t = T.truncate_head(text)
    assert t.truncated and t.truncated_by == "lines"
    assert t.output_lines == T.DEFAULT_MAX_LINES
    assert t.content.startswith("line1\n")
    assert t.content.endswith("line2000")


# ------------------------------------------------------------------ byte limit

def test_tail_byte_limit_multibyte_boundary() -> None:
    # 100-byte lines of multi-byte UTF-8: é is 2 bytes, 50 per line.
    line = "é" * 50
    text = "\n".join(line for _ in range(1000))  # ~101KB, 1000 lines
    t = T.truncate_tail(text)
    assert t.truncated and t.truncated_by == "bytes"
    assert t.output_bytes <= T.DEFAULT_MAX_BYTES
    assert not t.last_line_partial
    # Only whole lines, all intact.
    assert all(l == line for l in t.content.split("\n"))


def test_tail_giant_single_line_partial() -> None:
    text = "x" * 200_000  # one line, 4x the byte budget
    t = T.truncate_tail(text)
    assert t.truncated and t.truncated_by == "bytes"
    assert t.last_line_partial
    assert t.output_lines == 1
    assert t.output_bytes == T.DEFAULT_MAX_BYTES
    assert t.content == "x" * T.DEFAULT_MAX_BYTES


def test_tail_giant_line_utf8_boundary_walk() -> None:
    # Multi-byte chars: the cut must land on a character boundary.
    text = "é" * 100_000  # 200KB, one line
    t = T.truncate_tail(text)
    assert t.last_line_partial
    assert t.output_bytes <= T.DEFAULT_MAX_BYTES
    assert set(t.content) == {"é"}  # no mojibake at the cut


def test_head_first_line_exceeds_limit() -> None:
    text = "y" * 200_000 + "\nsecond line"
    t = T.truncate_head(text)
    assert t.truncated and t.truncated_by == "bytes"
    assert t.first_line_exceeds_limit
    assert t.content == ""
    assert t.output_lines == 0


# ------------------------------------------------------------------ format_size

def test_format_size() -> None:
    assert T.format_size(512) == "512B"
    assert T.format_size(50 * 1024) == "50.0KB"
    assert T.format_size(int(1.2 * 1024 * 1024)) == "1.2MB"


# --------------------------------------------------------- cap_tail_for_model

@pytest.fixture
def pai_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(PA, "PAI_ROOT", tmp_path, raising=True)
    return tmp_path


def test_cap_tail_spills_and_footers(pai_root: Path) -> None:
    text = "\n".join(f"line{i}" for i in range(1, 6201))
    out = T.cap_tail_for_model(text, slug="root", tool="bash")
    assert out.startswith("line4201\n")
    assert "[Showing lines 4201-6200 of 6200. Full output: /tmp/bash-root-" in out
    # Spill file exists under PAI_ROOT/tmp and holds the full text.
    spills = list((pai_root / "tmp").glob("bash-root-*.log"))
    assert len(spills) == 1
    assert spills[0].read_text(encoding="utf-8") == text
    # Footer path round-trips: /tmp/<name> == PAI_ROOT/tmp/<name>.
    cited = out.rsplit("Full output: ", 1)[1].rstrip("]")
    assert cited == f"/tmp/{spills[0].name}"


def test_cap_tail_byte_limit_footer(pai_root: Path) -> None:
    line = "z" * 100
    text = "\n".join(line for _ in range(1000))  # ~101KB
    out = T.cap_tail_for_model(text, slug="root", tool="shell")
    assert "(50.0KB limit). Full output: /tmp/shell-root-" in out


def test_cap_tail_giant_line_footer(pai_root: Path) -> None:
    text = "q" * 200_000
    out = T.cap_tail_for_model(text, slug="root", tool="bash")
    assert "[Showing last 50.0KB of line 1 (line is 195.3KB). Full output: /tmp/bash-root-" in out


def test_cap_tail_untruncated_writes_no_file(pai_root: Path) -> None:
    out = T.cap_tail_for_model("small output", slug="root", tool="bash")
    assert out == "small output"
    assert not (pai_root / "tmp").exists()


def test_cap_tail_sanitizes_slug(pai_root: Path) -> None:
    T.cap_tail_for_model("x\n" * 3000, slug="?", tool="bash")
    spills = list((pai_root / "tmp").glob("*.log"))
    assert len(spills) == 1
    assert "?" not in spills[0].name
