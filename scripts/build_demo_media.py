"""Maintainer tool: assemble the README media from the synthetic dashboard.

Two products, both from fixtures, neither touching the network or real data:

* **The four README stills** (`docs/dashboard.png`, `docs/tracker.png`,
  `docs/resume-data.png`, `docs/settings.png`) are stamped from the PNGs that
  `scripts/ui_screenshots.py` writes into the gitignored `.screenshots/`. Run

      python scripts/ui_screenshots.py p8

  first, then pass the same prefix here.

* **`docs/demo.gif`** is rendered live, from the storyboard in
  `scripts/build_walkthrough.py` -- the same scene list the MP4 uses, re-timed
  for a loop. It changes state inside a screen (row selection, a live search
  filter) instead of cutting between eight static tabs, so it reads as motion.

    python scripts/build_demo_media.py p8

Every frame carries the "representative sample data" watermark and the fixtures
are fictional (Acme / Globex / Initech / Hooli / Vandelay, run label
"synthetic", Jane Doe's example résumé). Keep it that way: no grab of this
dashboard may ever show a real posting or the author's own data.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

REPO = Path(__file__).resolve().parent.parent
SHOTS = REPO / ".screenshots"
DOCS = REPO / "docs"

BAND_H = 34
BG = (13, 17, 23)
FG = (139, 148, 158)
WATERMARK = "representative sample data"

# The README's Screenshots grid: four distinct screens, not four near-duplicates
# of the same table. (slug in .screenshots, caption band, committed filename)
STILLS = [
    ("high_score", "High Score - LLM-ranked postings with a score breakdown",
     "dashboard.png"),
    ("tracker", "Tracker - application statuses + follow-up nudges",
     "tracker.png"),
    ("resume_data", "Resume Data - the atoms every bullet must trace back to",
     "resume-data.png"),
    ("settings", "Settings - every key, path and option, no file editing",
     "settings.png"),
]

# GIF timing. Holds are far shorter than the MP4's (a loop that lingers reads as
# a slideshow), and the crossfade runs at 25 fps so the cut itself is motion.
FIRST_HOLD_MS = 2000
HOLD_MS = 1500
FADE_FRAMES = 10
FADE_MS = 40
GIF_COLORS = 128


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
    draw.text((16, y), f"INployed - {caption}", font=font, fill=FG, anchor="lm")
    draw.text((w - 16, y), WATERMARK, font=font, fill=FG, anchor="rm")
    return out


def build_stills(prefix: str) -> list[tuple[Path, int]]:
    written = []
    for slug, caption, name in STILLS:
        src = SHOTS / f"{prefix}_{slug}_1.0.png"
        if not src.exists():
            raise SystemExit(
                f"missing {src}\nRun: python scripts/ui_screenshots.py {prefix}")
        out = DOCS / name
        _stamp(src, caption).save(out, optimize=True)
        written.append((out, out.stat().st_size))
    return written


def build_gif() -> Path:
    """Render the walkthrough storyboard live and write it as a looping GIF."""
    sys.path.insert(0, str(REPO / "scripts"))
    import build_walkthrough as bw

    tmp = tempfile.TemporaryDirectory(prefix="demo_gif_")
    holds, _secs = bw.render_scenes(Path(tmp.name))
    if not holds:
        raise SystemExit("no frames captured")

    frames: list[Image.Image] = []
    durations: list[int] = []
    for i, hold in enumerate(holds):
        frames.append(hold)
        durations.append(FIRST_HOLD_MS if i == 0 else HOLD_MS)
        nxt = holds[(i + 1) % len(holds)]  # the tour loops, so the last fades to the first
        for k in range(1, FADE_FRAMES + 1):
            frames.append(Image.blend(hold, nxt, k / (FADE_FRAMES + 1)))
            durations.append(FADE_MS)

    # One shared adaptive palette: the theme is flat and dark, so 128 colours
    # cost nothing visually and let every frame reuse the same table.
    palette = frames[0].quantize(colors=GIF_COLORS, method=Image.Quantize.MAXCOVERAGE)
    quantized = [f.quantize(palette=palette, dither=Image.Dither.NONE) for f in frames]

    gif = DOCS / "demo.gif"
    quantized[0].save(
        gif,
        save_all=True,
        append_images=quantized[1:],
        duration=durations,
        loop=0,
        optimize=True,
        disposal=1,   # keep the previous frame: lets optimize() write deltas only
    )
    secs = sum(durations) / 1000
    print(f"{gif} -> {len(holds)} scenes, {len(frames)} frames, {secs:.0f}s, "
          f"{frames[0].width}x{frames[0].height}, {gif.stat().st_size / 1e6:.2f} MB")
    return gif


def main() -> int:
    prefix = sys.argv[1] if len(sys.argv) > 1 else "current"
    total = 0
    for path, size in build_stills(prefix):
        total += size
        print(f"{path} -> {size / 1e3:.0f} KB")
    gif = build_gif()
    total += gif.stat().st_size
    print(f"committed README media total: {total / 1e6:.2f} MB")
    if gif.stat().st_size > 15e6:
        print("WARNING: demo.gif is over the 15 MB budget.")
    if total > 30e6:
        print("WARNING: committed README media is over the 30 MB budget.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
