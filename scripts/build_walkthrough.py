"""Maintainer tool: render the README walkthrough video from the real dashboard.

Same idea as `scripts/ui_screenshots.py` -- it builds the real MainWindow
offscreen against that module's synthetic fixtures -- but instead of one grab per
tab it drives a scripted tour (select a job, read its score breakdown, filter,
walk the tracker, look at the run metrics) and encodes the result as an MP4.

    python scripts/build_walkthrough.py

Writes `docs/walkthrough.mp4`. The video is deliberately NOT committed: GitHub
renders an inline player only for files uploaded to its user-content CDN, and the
repo has no business carrying a multi-megabyte binary it does not need. Upload
steps are printed when the encode finishes.

Requires `imageio-ffmpeg` (maintainer-only, not in requirements.txt):

    pip install imageio-ffmpeg

Nothing here touches the network, the user's data, or any paid API. The résumé
tailoring step is represented by its inputs (the Resume Data tab), not by a live
run -- a real run costs API credits and would put real personal data on screen.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import ui_screenshots as uis  # noqa: E402  (sets QT_QPA_PLATFORM/QT_QPA_FONTDIR)

import pandas as pd  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402
from PySide6 import QtWidgets  # noqa: E402

DOCS = REPO / "docs"
OUT = DOCS / "walkthrough.mp4"

WIN_W, WIN_H = 1600, 1000
BAND_H = 44
BG = (13, 17, 23)
INK = (230, 237, 243)
MUTED = (139, 148, 158)

FPS = 30
FADE_FRAMES = 8
WATERMARK = "representative sample data"


# --- captions ---------------------------------------------------------------

def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    names = ("segoeuib.ttf", "arialbd.ttf") if bold else ("segoeui.ttf", "arial.ttf")
    for name in (*names, "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def _band(shot: Image.Image, caption: str) -> Image.Image:
    out = Image.new("RGB", (shot.width, shot.height + BAND_H), BG)
    out.paste(shot, (0, 0))
    d = ImageDraw.Draw(out)
    y = shot.height + BAND_H // 2
    d.text((18, y), "INployed", font=_font(17, bold=True), fill=INK, anchor="lm")
    d.text((18 + 86, y), caption, font=_font(17), fill=MUTED, anchor="lm")
    d.text((out.width - 18, y), WATERMARK, font=_font(16), fill=MUTED, anchor="rm")
    return out


# --- the tour ---------------------------------------------------------------
# (tab title, action on the window, caption, seconds on screen)

def _tab(win, title):
    win.tabs.setCurrentWidget(win._tab_widgets[title])


def _pick(tab, row: int) -> None:
    tab.table.clearSelection()
    tab.table.selectRow(row)


def _search(win, text: str) -> None:
    win.high_tab.search.setText(text)
    win.high_tab._apply_filters()   # the live box debounces; the tour cannot wait


def scenes(win):
    return [
        ("High Score (Unseen)", lambda: _pick(win.high_tab, 0),
         "High Score - what a scored run leaves you to actually look at", 6.0),
        ("High Score (Unseen)", lambda: _pick(win.high_tab, 1),
         "Every row carries the model's reason, strengths and gaps", 5.5),
        ("High Score (Unseen)", lambda: _pick(win.high_tab, 2),
         "Colour tracks tailoring state: tailored, failed, untouched", 5.0),
        ("High Score (Unseen)", lambda: _search(win, "engineer"),
         "Search and filter narrow the list without a re-run", 5.0),
        ("High Score (Unseen)", lambda: (_search(win, ""), _pick(win.high_tab, 0)),
         "Clearing the filter restores the ranked list", 3.5),
        ("All Jobs", lambda: _pick(win.all_tab, 0),
         "All Jobs - every posting collected, scored or not", 5.5),
        ("Tracker", lambda: _pick(win.tracker_tab, 0),
         "Tracker - application status, with follow-ups flagged when due", 6.5),
        ("Tracker", lambda: _pick(win.tracker_tab, 3),
         "Statuses run applied through interviewing, offer and rejected", 5.0),
        ("Auto-apply", lambda: None,
         "Auto-apply queue - batch tailoring that stops short of submitting", 6.0),
        ("Stats", lambda: None,
         "Stats - per-run counts, token spend and rescore outcomes", 6.5),
        ("Resume Data", lambda: None,
         "Resume Data - the atoms every generated bullet must trace back to", 7.0),
        ("Apply Answers", lambda: None,
         "Apply Answers - reusable responses for application forms", 5.5),
        ("Settings", lambda: None,
         "Settings - keys, paths, schedule and engine options, no file editing", 6.5),
    ]


# --- encode -----------------------------------------------------------------

def _encode(entries, out: Path) -> None:
    """entries: [(png_path, seconds)]. Uses the concat demuxer so a still hold
    costs one PNG instead of `fps * seconds` piped frames."""
    from imageio_ffmpeg import get_ffmpeg_exe

    listing = out.parent / "_concat.txt"
    lines = []
    for p, secs in entries:
        lines.append(f"file '{p.as_posix()}'")
        lines.append(f"duration {secs:.4f}")
    lines.append(f"file '{entries[-1][0].as_posix()}'")  # concat drops the last duration
    listing.write_text("\n".join(lines), encoding="utf-8")

    cmd = [get_ffmpeg_exe(), "-y", "-loglevel", "error",
           "-f", "concat", "-safe", "0", "-i", str(listing),
           "-vf", f"fps={FPS},format=yuv420p",
           "-c:v", "libx264", "-preset", "slow", "-crf", "24",
           "-movflags", "+faststart", str(out)]
    subprocess.run(cmd, check=True)
    listing.unlink()


def main() -> int:
    tmp = tempfile.TemporaryDirectory(prefix="walkthrough_")
    tmp_dir = Path(tmp.name)
    frames_dir = tmp_dir / "frames"
    frames_dir.mkdir()

    # Same synthetic world the screenshots use: fictional jobs, a fictional
    # master_experience.yaml, and a .env full of placeholders.
    queue_path = tmp_dir / "apply_queue.json"
    uis._write_queue(queue_path, [])
    import os
    os.environ["APPLY_QUEUE_PATH"] = str(queue_path)
    resume_dir = tmp_dir / "resume_j1"
    resume_dir.mkdir()
    uis._sanitize_personal_tabs(tmp_dir)

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from qt import theme
    from qt.main_window import MainWindow
    theme.apply_theme(app)
    theme.set_scale(app, 1.0)
    win = MainWindow(csv_paths=[], registry=uis._registry(resume_dir))
    win.show()
    win.resize(WIN_W, WIN_H)
    app.processEvents()

    win.min_score = 4
    win.df = uis._jobs_df()
    stats_path = tmp_dir / "run_stats.csv"
    uis._write_run_stats(stats_path)
    from qt import main_window as _mw
    _mw.gdrive_root_dir = lambda _paths: tmp_dir
    win._stats_df = pd.read_csv(stats_path)
    win._apply_df_views()
    win._refresh_stats()
    uis._write_queue(queue_path, uis._queue_jobs())
    win.apply_queue_panel.refresh()
    app.processEvents()

    holds: list[Image.Image] = []
    secs: list[float] = []
    for title, action, caption, dur in scenes(win):
        _tab(win, title)
        app.processEvents()
        action()
        win.resize(WIN_W, WIN_H)
        app.processEvents()
        raw = tmp_dir / "raw.png"
        if not win.grab().save(str(raw)):
            print(f"NOTE: grab failed for {caption!r}")
            continue
        holds.append(_band(Image.open(raw).convert("RGB"), caption))
        secs.append(dur)

    if not holds:
        print("no frames captured")
        return 1

    # Write holds + crossfade frames as PNGs, then let ffmpeg time them.
    entries: list[tuple[Path, float]] = []
    fade_dur = 1.0 / FPS
    for i, (hold, dur) in enumerate(zip(holds, secs)):
        p = frames_dir / f"hold_{i:02d}.png"
        hold.save(p)
        entries.append((p, dur))
        if i + 1 < len(holds):
            for k in range(1, FADE_FRAMES + 1):
                fp = frames_dir / f"fade_{i:02d}_{k:02d}.png"
                Image.blend(hold, holds[i + 1], k / (FADE_FRAMES + 1)).save(fp)
                entries.append((fp, fade_dur))

    DOCS.mkdir(exist_ok=True)
    _encode(entries, OUT)
    total = sum(d for _, d in entries)
    mb = OUT.stat().st_size / 1e6
    print(f"{OUT} -> {len(holds)} scenes, {total:.0f}s, {mb:.1f} MB, "
          f"{holds[0].width}x{holds[0].height}")
    if mb > 10:
        print("WARNING: GitHub caps comment-box video uploads at 10 MB.")
    print("\nTo publish it (browser only -- there is no API for this):")
    print("  1. Open any issue or PR comment box on github.com/yib7/INployed.")
    print(f"  2. Drag {OUT.name} into it. GitHub uploads it and inserts a")
    print("     https://github.com/user-attachments/assets/<uuid> URL.")
    print("  3. Copy that URL, then close the box WITHOUT commenting.")
    print("  4. Paste it under '## Demo' in README.md, below the GIF.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
