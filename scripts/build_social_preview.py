"""Maintainer tool: build the GitHub social preview card from the hero screenshot.

GitHub renders the social preview at 1280x640 (2:1) and link unfurls shrink it
hard -- often to ~600 px wide in a chat client -- so an as-is upload of the
1600x1034 `docs/dashboard.png` letterboxes and its 15 px UI text turns to mush.
This composes a card instead: the screenshot as a darkened, blurred backdrop with
the project name and one-line description set large enough to survive the shrink.

    python scripts/build_social_preview.py

Reads `docs/dashboard.png` (regenerate it with `scripts/build_demo_media.py`) and
writes `docs/social-preview.png`. Nothing here touches the network or real data.

The upload itself is manual: GitHub exposes the social preview only in the web UI
(Settings -> General -> Social preview -> Edit -> Upload an image), with no REST
or `gh` equivalent.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

REPO = Path(__file__).resolve().parent.parent
DOCS = REPO / "docs"
SRC = DOCS / "dashboard.png"
OUT = DOCS / "social-preview.png"

W, H = 1280, 640
PAD = 72

BG = (13, 17, 23)          # the app's own window background
INK = (240, 246, 252)
MUTED = (139, 148, 158)
ACCENT = (88, 166, 255)    # the dashboard's link/primary blue

TITLE = "INployed"
TAGLINE = "Job discovery & résumé tailoring, end to end."
BULLETS = [
    "Scheduled cloud scraper → two-stage Gemini scorer → desktop triage",
    "One-click LaTeX résumé built only from facts you wrote yourself",
]
FOOTER = "github.com/yib7/INployed"

# Crop the top of the hero shot: header, tab bar and the ranked job table. The
# lower half (detail card, button row) is the least legible part once blurred.
CROP_TOP = 0
CROP_H = 800


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    names = ("segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf") if bold else (
        "segoeui.ttf", "arial.ttf", "DejaVuSans.ttf")
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def _backdrop() -> Image.Image:
    shot = Image.open(SRC).convert("RGB")
    shot = shot.crop((0, CROP_TOP, shot.width, CROP_TOP + CROP_H))
    shot = shot.resize((W, H), Image.LANCZOS)
    shot = shot.filter(ImageFilter.GaussianBlur(3.5))
    return ImageEnhance.Brightness(shot).enhance(0.38)


def _scrim(card: Image.Image) -> Image.Image:
    """Darken the left edge further so the text block always clears its backdrop,
    whatever the screenshot happens to show there."""
    grad = Image.new("L", (W, 1))
    px = grad.load()
    for x in range(W):
        px[x, 0] = int(205 * max(0.0, 1.0 - (x / (W * 0.82))) ** 1.25)
    mask = grad.resize((W, H))
    return Image.composite(Image.new("RGB", (W, H), BG), card, mask)


def main() -> int:
    card = _scrim(_backdrop())
    draw = ImageDraw.Draw(card)

    # "IN" in the accent colour, the rest in white, matching the app's wordmark.
    f_title = _font(78, bold=True)
    x = PAD
    y = 168
    draw.text((x, y), "IN", font=f_title, fill=ACCENT)
    x += draw.textlength("IN", font=f_title)
    draw.text((x, y), "ployed", font=f_title, fill=INK)

    draw.line([(PAD, y + 118), (PAD + 96, y + 118)], fill=ACCENT, width=5)

    draw.text((PAD, y + 150), TAGLINE, font=_font(34), fill=INK)

    by = y + 216
    for line in BULLETS:
        draw.text((PAD, by), line, font=_font(23), fill=MUTED)
        by += 38

    draw.text((PAD, H - PAD - 8), FOOTER, font=_font(21), fill=MUTED)

    card.save(OUT, optimize=True)
    kb = OUT.stat().st_size / 1e3
    print(f"{OUT} -> {card.width}x{card.height}, {kb:.0f} KB")
    if kb > 1000:
        print("WARNING: GitHub rejects social previews over 1 MB.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
