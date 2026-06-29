"""Expand markdown image refs into Anthropic content blocks.

Any text destined for the LLM (user turn body, tool_result stdout, peer
message body) is scanned for `![alt](path)`. Matched paths are read,
base64-encoded, and spliced in as image blocks alongside the surrounding
text. No marker → original string returned untouched (fast path).

Path resolution: tilde-expanded, relative paths resolved against the
caller-supplied `base_dir`. Resolved paths must stay inside `PAI_ROOT`;
escapes are left as literal text so the model sees what was attempted.
Missing files, unreadable bytes, and unsupported media types also pass
through as literal text — image expansion never fails the turn.
"""

from __future__ import annotations

import base64
import re
from io import BytesIO
from pathlib import Path
from typing import Union

from .paths import PAI_ROOT

try:
    from PIL import Image as _PILImage  # type: ignore
except Exception:
    _PILImage = None

_IMAGE_REF_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")

# Anthropic-supported media types.
_EXT_TO_MEDIA = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

# Soft caps. Longest edge for Pillow downscale; byte budget when Pillow
# isn't available.
_MAX_EDGE = 1568
_MAX_BYTES = 5 * 1024 * 1024


def _resolve_path(ref: str, base_dir: Path | None) -> Path | None:
    """Resolve a markdown ref to an absolute path inside PAI_ROOT.

    Returns None if the path escapes PAI_ROOT (caller leaves the ref as
    literal text).
    """
    p = Path(ref).expanduser()
    if not p.is_absolute() and base_dir is not None:
        p = base_dir / p
    try:
        resolved = p.resolve()
        root = PAI_ROOT.resolve()
    except OSError:
        return None
    try:
        resolved.relative_to(root)
    except ValueError:
        return None
    return resolved


def _load_and_encode(path: Path) -> tuple[str, str] | None:
    """Return (media_type, base64-encoded data) or None if unusable."""
    media_type = _EXT_TO_MEDIA.get(path.suffix.lower())
    if media_type is None:
        return None
    try:
        raw = path.read_bytes()
    except OSError:
        return None

    if _PILImage is not None:
        try:
            with _PILImage.open(BytesIO(raw)) as img:
                w, h = img.size
                longest = max(w, h)
                if longest > _MAX_EDGE:
                    scale = _MAX_EDGE / longest
                    new_size = (max(1, int(w * scale)), max(1, int(h * scale)))
                    img = img.resize(new_size, _PILImage.LANCZOS)
                    buf = BytesIO()
                    # Map media_type → Pillow format.
                    fmt = {
                        "image/png": "PNG",
                        "image/jpeg": "JPEG",
                        "image/gif": "GIF",
                        "image/webp": "WEBP",
                    }[media_type]
                    save_kwargs: dict = {}
                    if fmt == "JPEG" and img.mode in ("RGBA", "P"):
                        img = img.convert("RGB")
                    img.save(buf, format=fmt, **save_kwargs)
                    raw = buf.getvalue()
        except Exception:
            # Fall through with the raw bytes; size cap below may still drop it.
            pass

    if len(raw) > _MAX_BYTES:
        return None

    return media_type, base64.standard_b64encode(raw).decode("ascii")


def expand_image_refs(
    text: str, *, base_dir: Path | None = None
) -> Union[str, list[dict]]:
    """Scan `text` for `![](path)` refs and splice in image blocks.

    No refs → return the original string. Otherwise return a list of
    Anthropic content blocks alternating text/image (empty text segments
    dropped). Bad refs (missing, unreadable, unsupported, outside
    PAI_ROOT) are left as literal text inside their text block.
    """
    if not text or "![" not in text:
        return text

    matches = list(_IMAGE_REF_RE.finditer(text))
    if not matches:
        return text

    blocks: list[dict] = []
    cursor = 0
    pending_text = ""

    def flush_text() -> None:
        nonlocal pending_text
        if pending_text:
            blocks.append({"type": "text", "text": pending_text})
            pending_text = ""

    any_image = False
    for m in matches:
        pending_text += text[cursor:m.start()]
        cursor = m.end()
        ref = m.group(2).strip()
        path = _resolve_path(ref, base_dir)
        encoded = _load_and_encode(path) if path is not None else None
        if encoded is None:
            # Leave the marker as literal text.
            pending_text += m.group(0)
            continue
        media_type, data = encoded
        flush_text()
        blocks.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": data,
            },
        })
        any_image = True

    pending_text += text[cursor:]
    flush_text()

    if not any_image:
        return text
    return blocks


def _placeholder(block: dict) -> dict:
    """Text stand-in for a base64 image block: media type + rough size."""
    src = block.get("source", {})
    media_type = src.get("media_type", "image")
    data = src.get("data", "")
    kb = len(data) * 3 / 4 / 1024
    return {"type": "text", "text": f"[image elided from history: {media_type}, ~{kb:.0f}KB]"}


def _is_base64_image(block: object) -> bool:
    return (
        isinstance(block, dict)
        and block.get("type") == "image"
        and isinstance(block.get("source"), dict)
        and block["source"].get("type") == "base64"
    )


def dehydrate_image_blocks(messages: list[dict]) -> list[dict]:
    """Return a copy of `messages` with base64 image blocks replaced by text.

    The inverse of `expand_image_refs`: base64 is meant to be ephemeral (sent
    to the API for one turn), never frozen into the on-disk transcript where it
    bloats the file ~400x and is a landmine for any reader that treats it as
    text. Each `{"type":"image","source":{"type":"base64",...}}` block becomes a
    small `[image elided ...]` placeholder.

    Non-mutating: only the messages/blocks that actually carry images are
    deep-copied and rewritten; everything else is passed through by reference.
    Recurses one level into `tool_result` blocks whose `content` is a list
    (where browse screenshots land). Only user-role content carries images, so
    assistant turns are untouched in practice.
    """
    out: list[dict] = []
    for msg in messages:
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            out.append(msg)
            continue
        new_content, changed = _dehydrate_blocks(content)
        if changed:
            new_msg = dict(msg)
            new_msg["content"] = new_content
            out.append(new_msg)
        else:
            out.append(msg)
    return out


def _dehydrate_blocks(blocks: list) -> tuple[list, bool]:
    """Return (possibly-new block list, changed?) with images dehydrated.

    Recurses one level into tool_result blocks whose content is a list.
    """
    new_blocks: list = []
    changed = False
    for block in blocks:
        if _is_base64_image(block):
            new_blocks.append(_placeholder(block))
            changed = True
        elif (
            isinstance(block, dict)
            and block.get("type") == "tool_result"
            and isinstance(block.get("content"), list)
        ):
            inner, inner_changed = _dehydrate_blocks(block["content"])
            if inner_changed:
                new_block = dict(block)
                new_block["content"] = inner
                new_blocks.append(new_block)
                changed = True
            else:
                new_blocks.append(block)
        else:
            new_blocks.append(block)
    return new_blocks, changed
