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


# --- dehydrate_image_blocks ---

from boot.image_refs import dehydrate_image_blocks

_B64 = base64.standard_b64encode(_PNG_BYTES).decode("ascii")


def _image_block() -> dict:
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": _B64},
    }


def test_dehydrate_top_level_image_block():
    messages = [{"role": "user", "content": [_image_block()]}]
    out = dehydrate_image_blocks(messages)
    block = out[0]["content"][0]
    assert block["type"] == "text"
    assert block["text"].startswith("[image elided from history:")
    assert "image/png" in block["text"]
    assert "data" not in block


def test_dehydrate_image_nested_in_tool_result():
    messages = [{
        "role": "user",
        "content": [{
            "type": "tool_result",
            "tool_use_id": "t-1",
            "content": [
                {"type": "text", "text": "screenshot:"},
                _image_block(),
            ],
        }],
    }]
    out = dehydrate_image_blocks(messages)
    inner = out[0]["content"][0]["content"]
    assert inner[0] == {"type": "text", "text": "screenshot:"}
    assert inner[1]["type"] == "text"
    assert inner[1]["text"].startswith("[image elided from history:")
    assert "data" not in inner[1]


def test_dehydrate_no_images_unchanged():
    messages = [
        {"role": "user", "content": "plain string"},
        {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
    ]
    out = dehydrate_image_blocks(messages)
    assert out == messages


def test_dehydrate_does_not_mutate_input():
    block = _image_block()
    messages = [{"role": "user", "content": [block]}]
    out = dehydrate_image_blocks(messages)
    # Input untouched: still a base64 image with its data.
    assert messages[0]["content"][0]["source"]["data"] == _B64
    assert block["type"] == "image"
    # Output is a different object.
    assert out[0]["content"][0] is not block


def test_dehydrate_placeholder_is_base64_free():
    messages = [{"role": "user", "content": [_image_block()]}]
    out = dehydrate_image_blocks(messages)
    text = out[0]["content"][0]["text"]
    assert _B64 not in text
    assert "image/png" in text
    assert "KB]" in text
