"""Render Bhajan Studio's app icons (static/icons/) from its brand mark.

usage: .venv/bin/python tools/make_icons.py [--font PATH] [--out DIR]

The mark is the Devanagari "ॐ" (U+0950) in the accent colour #c8691c on the cream page colour #faf7f2
(the .brand-mark in static/index.html). The PNGs are committed, so run this only if the mark changes.

Font: Kohinoor Devanagari Bold (Apple, /System/Library/Fonts/Kohinoor.ttc) on macOS; on Linux, Noto Sans
Devanagari (SIL Open Font License). The README credits it. The script prints the font it actually used.

Only PNG renderings are made: a vector favicon would have to carry the font's own outline, and Kohinoor
is not freely licensed.
"""
import argparse
import os
import sys

from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OM = "ॐ"
MISSING = "\U0010fffd"   # a private-use codepoint no font draws: what a missing glyph looks like
ACCENT = "#c8691c"
CREAM = "#faf7f2"
FONTS = [
    "/System/Library/Fonts/Kohinoor.ttc",
    "/System/Library/Fonts/Supplemental/Devanagari Sangam MN.ttc",
    "/System/Library/Fonts/Supplemental/ITFDevanagari.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansDevanagari-Regular.ttf",
]


def find_font(arg):
    if arg:
        if not os.path.isfile(arg):
            sys.exit(f"Font not found: {arg}")
        return arg
    for path in FONTS:
        if os.path.isfile(path):
            return path
    sys.exit("No Devanagari font found. Install one (e.g. Noto Sans Devanagari) or pass --font PATH.")


def pick_face(path):
    """Face index to use: in a .ttc collection prefer a Bold face, since a heavier mark stays legible at 32 px."""
    if not path.lower().endswith((".ttc", ".otc")):
        return 0
    faces = []
    for index in range(32):
        try:
            faces.append(ImageFont.truetype(path, 100, index=index).getname())
        except OSError:
            break
    for want in ("bold", "semibold", "demi"):
        for index, (_, style) in enumerate(faces):
            if style.lower() == want:
                return index
    return 0


def ink_of(font, text):
    """The ink of `text` as an "L" mask cropped to its bounding box (None if nothing is drawn)."""
    pad = font.size
    left, top, right, bottom = font.getbbox(text)
    canvas = Image.new("L", (right - left + 2 * pad, bottom - top + 2 * pad), 0)
    ImageDraw.Draw(canvas).text((pad - left, pad - top), text, font=font, fill=255)
    box = canvas.getbbox()
    return canvas.crop(box) if box else None


def check_glyph(font, path):
    """Stop if the font draws a blank or a "tofu" box instead of the real glyph."""
    ink, missing = ink_of(font, OM), ink_of(font, MISSING)
    if ink is None:
        sys.exit(f"{path} draws nothing for U+0950 (ॐ); pass --font with a Devanagari font.")
    if missing is not None and (ink.size, ink.tobytes()) == (missing.size, missing.tobytes()):
        sys.exit(f"{path} has no glyph for U+0950 (ॐ; it draws the 'missing' box); pass --font with a Devanagari font.")


def mark_for(path, index, side):
    """The glyph's ink (an "L" mask) scaled so its larger side is `side` pixels."""
    reference = ink_of(ImageFont.truetype(path, 1000, index=index), OM)
    size = max(1, round(1000 * side / max(reference.size)))
    return ink_of(ImageFont.truetype(path, size, index=index), OM)


def draw_icon(path, index, size, fraction, rounded, supersample=4):
    """One icon: the mark's ink box is centred (not the text origin) on a cream square."""
    big = size * supersample
    canvas = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    if rounded:
        draw.rounded_rectangle((0, 0, big - 1, big - 1), radius=round(big * 0.22), fill=CREAM)
    else:
        draw.rectangle((0, 0, big, big), fill=CREAM)
    ink = mark_for(path, index, round(big * fraction))
    canvas.paste(ACCENT, ((big - ink.width) // 2, (big - ink.height) // 2), ink)
    icon = canvas.resize((size, size), Image.LANCZOS)   # Pillow premultiplies alpha: no dark fringe on the corners
    return icon if rounded else icon.convert("RGB")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--font", help="a Devanagari font file (default: the first installed of the known ones)")
    ap.add_argument("--out", default=os.path.join(ROOT, "static", "icons"), help="output folder (default: static/icons)")
    args = ap.parse_args()

    path = find_font(args.font)
    index = pick_face(path)
    font = ImageFont.truetype(path, 100, index=index)
    check_glyph(font, path)
    os.makedirs(args.out, exist_ok=True)

    # name -> (size, ink fraction, rounded corners)
    icons = {
        "icon-192.png": (192, 0.62, True),
        "icon-512.png": (512, 0.62, True),
        "maskable-512.png": (512, 0.50, False),   # full bleed; the mark stays inside the 80% safe zone
        "apple-touch-icon-180.png": (180, 0.62, False),   # iOS rounds the corners itself and wants no transparency
        "favicon-32.png": (32, 0.74, True),
    }
    for name, (size, fraction, rounded) in icons.items():
        target = os.path.join(args.out, name)
        draw_icon(path, index, size, fraction, rounded, supersample=8 if size <= 64 else 4).save(target, optimize=True)
        print(f"{name:26s} {os.path.getsize(target):6d} bytes")
    print(f"font: {' '.join(font.getname())} ({path})")


if __name__ == "__main__":
    main()
