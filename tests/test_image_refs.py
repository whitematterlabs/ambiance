"""Tests for markdown image-ref expansion."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from boot import image_refs
from boot.image_refs import expand_image_refs


# 1x1 PNG.
_PNG_BYTES = base64.b64decode(
    b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(image_refs, "PAI_ROOT", tmp_path)
    return tmp_path


def _make_png(root: Path, name: str = "img.png") -> Path:
    p = root / name
    p.write_bytes(_PNG_BYTES)
    return p


def test_no_marker_passthrough(root):
    assert expand_image_refs("hello world", base_dir=root) == "hello world"


def test_empty_string(root):
    assert expand_image_refs("", base_dir=root) == ""


def test_one_marker_three_blocks(root):
    img = _make_png(root)
    text = f"before ![alt]({img}) after"
    out = expand_image_refs(text, base_dir=root)
    assert isinstance(out, list)
    assert len(out) == 3
    assert out[0] == {"type": "text", "text": "before "}
    assert out[1]["type"] == "image"
    assert out[1]["source"]["media_type"] == "image/png"
    assert out[1]["source"]["type"] == "base64"
    assert out[2] == {"type": "text", "text": " after"}


def test_marker_only_no_surrounding_text(root):
    img = _make_png(root)
    out = expand_image_refs(f"![]({img})", base_dir=root)
    assert isinstance(out, list)
    assert len(out) == 1
    assert out[0]["type"] == "image"


def test_missing_path_literal(root):
    text = "before ![alt](/nonexistent/path.png) after"
    out = expand_image_refs(text, base_dir=root)
    assert out == text


def test_escape_attempt_literal(root, tmp_path):
    # File outside PAI_ROOT.
    outside = tmp_path.parent / "outside.png"
    outside.write_bytes(_PNG_BYTES)
    try:
        text = f"x ![]({outside}) y"
        out = expand_image_refs(text, base_dir=root)
        assert out == text
    finally:
        outside.unlink(missing_ok=True)


def test_unsupported_media_type_literal(root):
    bad = root / "doc.pdf"
    bad.write_bytes(b"%PDF-1.4 not really")
    text = f"see ![]({bad})"
    out = expand_image_refs(text, base_dir=root)
    assert out == text


def test_relative_path_resolves_against_base_dir(root):
    img = _make_png(root, "rel.png")
    out = expand_image_refs("![](rel.png)", base_dir=root)
    assert isinstance(out, list)
    assert out[0]["type"] == "image"


def test_multiple_markers(root):
    a = _make_png(root, "a.png")
    b = _make_png(root, "b.png")
    text = f"![]({a}) middle ![]({b})"
    out = expand_image_refs(text, base_dir=root)
    assert isinstance(out, list)
    types = [b["type"] for b in out]
    assert types == ["image", "text", "image"]
