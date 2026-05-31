#!/usr/bin/env python3
"""Generate the PWA / add-to-home-screen icon set into public/.

PAI's add-to-home-screen icon: a terracotta tile (the web surface's accent,
`--accent` #c4663f) carrying a cream speech bubble with a three-dot typing
indicator — the same chat-bubble motif as the macOS menubar glyph. Full-bleed
so iOS/Android can apply their own rounded/maskable crop without clipping art.

Run from this directory:  python gen-icons.py
Requires Pillow (already a dev dep of the kernel venv).
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ACCENT = (196, 102, 63)   # --accent  #c4663f
CREAM = (244, 241, 234)   # theme/background  #f4f1ea
PUBLIC = Path(__file__).resolve().parent / "public"


def render(size: int) -> Image.Image:
    # Supersample 4x then downscale for crisp antialiased edges.
    s = size * 4
    img = Image.new("RGBA", (s, s), ACCENT + (255,))
    d = ImageDraw.Draw(img)

    # Speech bubble: rounded rect occupying the central ~62%, with a tail.
    pad = s * 0.19
    body = (pad, pad, s - pad, s - pad * 1.18)
    radius = s * 0.13
    d.rounded_rectangle(body, radius=radius, fill=CREAM + (255,))
    # Tail (lower-left), a triangle tucked under the bubble body.
    tx, ty = pad + s * 0.10, s - pad * 1.18
    d.polygon(
        [(tx, ty - s * 0.02), (tx + s * 0.14, ty - s * 0.02), (tx + s * 0.02, ty + s * 0.12)],
        fill=CREAM + (255,),
    )

    # Three typing dots, centered in the bubble body.
    cy = (body[1] + body[3]) / 2
    cx = (body[0] + body[2]) / 2
    dot = s * 0.035
    gap = s * 0.105
    for i in (-1, 0, 1):
        x = cx + i * gap
        d.ellipse((x - dot, cy - dot, x + dot, cy + dot), fill=ACCENT + (255,))

    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    PUBLIC.mkdir(parents=True, exist_ok=True)
    render(192).save(PUBLIC / "icon-192.png")
    render(512).save(PUBLIC / "icon-512.png")
    # apple-touch-icon: iOS ignores transparency and applies its own mask, so a
    # full-bleed 180px square is exactly right.
    render(180).save(PUBLIC / "apple-touch-icon.png")
    print(f"wrote icon-192.png, icon-512.png, apple-touch-icon.png → {PUBLIC}")


if __name__ == "__main__":
    main()
