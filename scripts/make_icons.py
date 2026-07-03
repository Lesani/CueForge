# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Lesani. See LICENSE for details.
"""Generate all CueForge icons from the master logo (assets/Logo.png).

The master is a photo-style render: a dark rounded square on a white page with
a soft drop shadow. This script crops to the rounded square, masks the corners
transparent, and emits:

- cueforge/web/icons/icon-512.png / icon-192.png   (transparent rounded square)
- cueforge/web/icons/icon-maskable-512.png          (full-bleed, safe-zone scaled)
- cueforge/web/icons/apple-touch-icon.png           (180px, opaque full-bleed)
- assets/CueForge.ico                               (16..256 multi-size, exe icon)

Run: .venv/Scripts/python.exe scripts/make_icons.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

ROOT = Path(__file__).resolve().parent.parent
MASTER = ROOT / "assets" / "Logo.png"
ICONS = ROOT / "cueforge" / "web" / "icons"
ICO_OUT = ROOT / "assets" / "CueForge.ico"

# Corner radius of the logo's rounded square, as a fraction of its width
# (measured from the master render; also matches Apple's squircle look).
CORNER_FRAC = 0.225
# Shave this fraction off the detected bounding box on each side to drop the
# anti-aliased white halo at the square's edge.
EDGE_SHAVE = 0.004


def find_dark_bbox(img: Image.Image) -> tuple[int, int, int, int]:
    """Bounding box of non-white content (the rounded square + its shadow is
    near-white at the edges, so threshold well below pure white)."""
    gray = img.convert("L")
    mask = gray.point(lambda v: 255 if v < 200 else 0)
    bbox = mask.getbbox()
    if not bbox:
        raise SystemExit("[FAILED] could not find logo content in " + str(MASTER))
    return bbox


def rounded_mask(size: int, radius_frac: float) -> Image.Image:
    m = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(m)
    d.rounded_rectangle([0, 0, size - 1, size - 1],
                        radius=int(size * radius_frac), fill=255)
    return m


def main() -> None:
    src = Image.open(MASTER).convert("RGB")
    left, top, right, bottom = find_dark_bbox(src)
    # The soft shadow widens the bbox slightly; shave in to the square proper.
    w = right - left
    h = bottom - top
    shave_x = int(w * EDGE_SHAVE) + 2
    shave_y = int(h * EDGE_SHAVE) + 2
    square = src.crop((left + shave_x, top + shave_y,
                       right - shave_x, bottom - shave_y))
    # Force 1:1 (the render is square, but guard against a lopsided bbox).
    square = ImageOps.fit(square, (1024, 1024), Image.LANCZOS)
    bg = square.getpixel((512, 8))  # sampled inside the top edge

    ICONS.mkdir(parents=True, exist_ok=True)

    def rounded_transparent(size: int) -> Image.Image:
        img = square.resize((size, size), Image.LANCZOS).convert("RGBA")
        img.putalpha(rounded_mask(size, CORNER_FRAC))
        return img

    rounded_transparent(512).save(ICONS / "icon-512.png")
    rounded_transparent(192).save(ICONS / "icon-192.png")

    # Maskable: full-bleed background, content scaled into the ~80% safe zone.
    size = 512
    canvas = Image.new("RGB", (size, size), bg)
    inner = square.resize((int(size * 0.86), int(size * 0.86)), Image.LANCZOS)
    off = (size - inner.width) // 2
    canvas.paste(inner, (off, off))
    canvas.save(ICONS / "icon-maskable-512.png")

    # Apple touch icon: opaque; iOS applies its own corner rounding, and the
    # logo's own dark corners blend into the matching full-bleed background.
    size = 180
    canvas = Image.new("RGB", (size, size), bg)
    canvas.paste(square.resize((size, size), Image.LANCZOS), (0, 0))
    canvas.save(ICONS / "apple-touch-icon.png")

    # Windows .ico for the exe (transparent rounded corners look right in the
    # taskbar / explorer, which do not round for you).
    base = rounded_transparent(256)
    base.save(ICO_OUT, sizes=[(256, 256), (128, 128), (64, 64),
                              (48, 48), (32, 32), (16, 16)])

    for p in [ICONS / "icon-512.png", ICONS / "icon-192.png",
              ICONS / "icon-maskable-512.png", ICONS / "apple-touch-icon.png",
              ICO_OUT]:
        print("[OK]", p.relative_to(ROOT))


if __name__ == "__main__":
    main()
