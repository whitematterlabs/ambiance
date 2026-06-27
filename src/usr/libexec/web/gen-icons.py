#!/usr/bin/env python3
"""Generate the PWA / add-to-home-screen icon set into public/.

PAI's add-to-home-screen icon: a terracotta tile (the web surface's accent,
`--accent` #c4663f) carrying a cream shell prompt — a chevron `❯` and a block
cursor — the same terminal mark as the header wordmark. PAI is PID 1: the icon
says "a system you talk to," not a chat app. Full-bleed so iOS/Android can apply
their own rounded/maskable crop without clipping art.

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

    stroke = s * 0.085          # chevron stroke weight
    mid = s * 0.5               # vertical center of the prompt line
    # Chevron `❯`: an open path top-left → apex (center-right) → bottom-left.
    cx_back = s * 0.30          # x of the two open ends
    cx_apex = s * 0.52          # x of the point
    half = s * 0.165            # vertical reach from center to each open end
    pts = [(cx_back, mid - half), (cx_apex, mid), (cx_back, mid + half)]
    d.line(pts, fill=CREAM + (255,), width=int(stroke), joint="curve")
    # Round the chevron's three vertices (PIL line caps/joints are square).
    r = stroke / 2
    for px, py in pts:
        d.ellipse((px - r, py - r, px + r, py + r), fill=CREAM + (255,))

    # Block cursor to the right of the chevron, like a waiting terminal caret.
    cur_w = s * 0.115
    cur_h = s * 0.30
    cur_x = s * 0.60
    d.rounded_rectangle(
        (cur_x, mid - cur_h / 2, cur_x + cur_w, mid + cur_h / 2),
        radius=s * 0.02,
        fill=CREAM + (255,),
    )

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
