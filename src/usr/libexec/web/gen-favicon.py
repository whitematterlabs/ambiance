#!/usr/bin/env python3
"""Generate the browser-tab favicon from the colorful PAI logo.

The favicon is the logo's left monogram (the interlocked-X mark) — the full
wordmark is illegible squished into a 16px tab, the monogram reads cleanly at
any size. It rides a rounded white tile so it has a defined edge on any tab
background, matching the logo's native white field. The gradient art is raster,
so we ship PNGs (a small 32px for crisp tabs + a 256px for high-DPI / shortcuts).

Source art: brand/pailogocolorful.png (1200x1200, white background).
Run from this directory:  python gen-favicon.py   (or: uv run python gen-favicon.py)
Requires Pillow (already a dev dep of the kernel venv).
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
SRC = HERE / "brand" / "pailogocolorful.png"
PUBLIC = HERE / "public"

# Monogram crop box in the 1200x1200 source (the left interlocked-X glyph).
MONO_BOX = (30, 395, 335, 805)
TILE = (255, 255, 255, 255)  # white field, matches the logo's native background
PAD = 0.16                   # mark inset from the tile edge
RADIUS = 0.20                # tile corner radius as a fraction of the side


def _to_alpha(img: Image.Image) -> Image.Image:
    """Knock the white background out to transparency."""
    img = img.convert("RGBA")
    px = img.load()
    w, h = img.size
    for y in range(h):
        for x in range(w):
            r, g, b, _ = px[x, y]
            if r > 245 and g > 245 and b > 245:
                px[x, y] = (r, g, b, 0)
    return img


def render(size: int, mark: Image.Image) -> Image.Image:
    # Supersample 4x for crisp antialiased tile corners and downscaled art.
    s = size * 4
    tile = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, s - 1, s - 1), radius=int(s * RADIUS), fill=255)
    Image.new("RGBA", (s, s), TILE).putalpha(mask)
    tile.paste(Image.new("RGBA", (s, s), TILE), (0, 0), mask)

    # Fit the monogram into the padded interior, preserving aspect.
    inner = int(s * (1 - PAD * 2))
    m = mark.copy()
    m.thumbnail((inner, inner), Image.LANCZOS)
    tile.alpha_composite(m, ((s - m.width) // 2, (s - m.height) // 2))
    return tile.resize((size, size), Image.LANCZOS)


def main() -> None:
    PUBLIC.mkdir(parents=True, exist_ok=True)
    mark = _to_alpha(Image.open(SRC)).crop(MONO_BOX)
    mark = mark.crop(mark.getbbox())  # tighten to the glyph
    render(256, mark).save(PUBLIC / "favicon.png")
    render(32, mark).save(PUBLIC / "favicon-32.png")
    print(f"wrote favicon.png, favicon-32.png → {PUBLIC}")


if __name__ == "__main__":
    main()
