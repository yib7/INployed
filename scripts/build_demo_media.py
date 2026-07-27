"""Maintainer tool: assemble the README media from the synthetic screenshots.

Run `python scripts/ui_screenshots.py p8` first -- it renders the real dashboard
offscreen against synthetic fixtures (no API calls, no real data), writing PNGs to
the gitignored `.screenshots/`. This script then stamps each tab shot with a caption
band and assembles them into `docs/demo.gif` (an 8-screen crossfaded tour) and
`docs/dashboard.png` (the still hero shot).

    python scripts/build_demo_media.py p8

Nothing here touches the network or the user's data.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

REPO = Path(__file__).resolve().parent.parent
SHOTS = REPO / ".screenshots"
DOCS = REPO / "docs"

BAND_H = 34
BG = (13, 17, 23)
FG = (139, 148, 158)
WATERMARK = "representative sample data"

# slug -> caption, in tour order.
TOUR = [
    ("high_score", "High Score - LLM-ranked postings with a score breakdown"),
    ("all_jobs", "All Jobs - every collected posting"),
    ("tracker", "Tracker - application statuses + follow-up nudges"),
    ("auto_apply", "Auto-apply - the batch apply queue"),
    ("stats", "Stats - per-run pipeline metrics"),
    ("resume_data", "Resume Data - the select-and-rephrase source of truth"),
    ("apply_answers", "Apply Answers - reusable form answers"),
    ("settings", "Settings - every option, no file editing"),
]

FIRST_HOLD_MS = 3200
HOLD_MS = 2400
FADE_FRAMES = 6
FADE_MS = 40


def _font() -> ImageFont.FreeTypeFont:
    for name in ("segoeui.ttf", "DejaVuSans.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, 15)
        except OSError:
            continue
    return ImageFont.load_default()


def _stamp(src: Path, caption: str) -> Image.Image:
    shot = Image.open(src).convert("RGB")
    w, h = shot.size
    out = Image.new("RGB", (w, h + BAND_H), BG)
    out.paste(shot, (0, 0))
    draw = ImageDraw.Draw(out)
    font = _font()
    y = h + BAND_H // 2
    draw.text((16, y), f"INployed — {caption}", font=font, fill=FG, anchor="lm")
    draw.text((w - 16, y), WATERMARK, font=font, fill=FG, anchor="rm")
    return out


def main() -> int:
    prefix = sys.argv[1] if len(sys.argv) > 1 else "current"
    holds = [_stamp(SHOTS / f"{prefix}_{slug}_1.0.png", cap) for slug, cap in TOUR]

    frames: list[Image.Image] = []
    durations: list[int] = []
    for i, hold in enumerate(holds):
        frames.append(hold)
        durations.append(FIRST_HOLD_MS if i == 0 else HOLD_MS)
        nxt = holds[(i + 1) % len(holds)]
        for k in range(1, FADE_FRAMES + 1):
            frames.append(Image.blend(hold, nxt, k / (FADE_FRAMES + 1)))
            durations.append(FADE_MS)
    # The tour loops, so drop the fade back into frame 0 only if you want a hard cut.

    gif = DOCS / "demo.gif"
    frames[0].save(
        gif,
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        loop=0,
        optimize=True,
        disposal=2,
    )
    holds[0].save(DOCS / "dashboard.png", optimize=True)
    print(f"{gif} -> {len(frames)} frames, {gif.stat().st_size / 1e6:.2f} MB")
    print(f"{DOCS / 'dashboard.png'} -> {(DOCS / 'dashboard.png').stat().st_size / 1e3:.0f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
