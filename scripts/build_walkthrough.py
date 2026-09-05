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

Nothing here touches the network, the user's data, or any paid API. The tour DOES
show the tailoring step, because it is the middle of the primary user journey, but
it shows a pre-built tailored folder (`ui_screenshots._tailored_folder`) rather
than a live run: a real run costs API credits and would put real personal data on
screen.
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


def _scroll(widget, steps: int) -> None:
    """Nudge a scrollable widget's vertical bar by `steps` of its own line step."""
    bar = widget.verticalScrollBar()
    bar.setValue(bar.value() + steps * max(1, bar.singleStep()))


def _scroll_frac(widget, frac: float) -> None:
    """Drive a scroll bar to `frac` of its own range.

    Fixed step counts are how a scroll beat goes dead: `scripts/build_demo_media`
    quantizes to a shared palette and Pillow then MERGES two identical frames into
    one long hold, so a step past the bottom silently turns a beat into a pause.
    A fraction of the live range always moves while the range is non-zero.
    """
    bar = widget.verticalScrollBar()
    lo, hi = bar.minimum(), bar.maximum()
    bar.setValue(round(lo + frac * (hi - lo)))


TYPED_QUERY = "engineer"


def scenes(win):
    """The tour, one entry per FRAME: (tab title, action, caption, seconds).

    Several entries share a caption on purpose. A step whose caption repeats the
    one before it is a beat inside the same scene rather than a new one, and it is
    what keeps the result moving: eight frames of a query being typed, with the
    table shedding rows underneath it, read as a person using the app. Thirteen
    frames each held for a second and a half read as a slide deck.
    """
    tailored = win._demo_tailored_folder   # planted by render_scenes, see below
    typing = "Search filters the ranked list live, without a re-run"
    tailor = "Tailor writes the resume, cover letter and apply sheet for THIS job"
    rows = "Every row carries the model's reason, strengths and gaps"
    sheet = "apply.md - one self-contained sheet, every bullet traced to your data"
    atoms = "Resume Data - the atoms every generated bullet must trace back to"
    queue = "Auto-apply queue - batch tailoring that stops short of submitting"
    runs = "Stats - per-run counts, token spend and rescore outcomes"
    every = "All Jobs - every posting collected, scored or not"
    statuses = "Statuses run applied through interviewing, offer and rejected"
    answers = "Apply Answers - reusable responses for application forms"
    knobs = "Settings - keys, paths, schedule and engine options, no file editing"
    return [
        ("High Score (Unseen)", lambda: _pick(win.high_tab, 0),
         "High Score - what a scored run leaves you to actually look at", 4.0),
        ("High Score (Unseen)", lambda: _pick(win.high_tab, 1), rows, 2.0),
        ("High Score (Unseen)", lambda: _pick(win.high_tab, 2), rows, 2.0),
        ("High Score (Unseen)", lambda: _pick(win.high_tab, 3), rows, 2.0),
        ("High Score (Unseen)", lambda: _pick(win.high_tab, 7),
         "Colour tracks tailoring state: tailored, failed, untouched", 2.6),
        # Typed one character at a time so the table visibly sheds rows.
        *[("High Score (Unseen)",
           (lambda n=n: _search(win, TYPED_QUERY[:n])), typing, 0.34)
          for n in range(1, len(TYPED_QUERY) + 1)],
        ("High Score (Unseen)", lambda: None, typing, 2.0),
        ("High Score (Unseen)", lambda: (_search(win, ""), _pick(win.high_tab, 7)),
         "Clearing it restores the ranked list", 2.4),
        # The journey's middle: tailor the selected job, then read what it wrote.
        ("High Score (Unseen)", lambda: uis._show_apply_panel(win, tailored),
         tailor, 4.2),
        ("High Score (Unseen)", lambda: _scroll(win.apply_panel._sheet, 5), sheet, 1.6),
        ("High Score (Unseen)", lambda: _scroll(win.apply_panel._sheet, 5), sheet, 1.6),
        ("High Score (Unseen)", lambda: _scroll(win.apply_panel._sheet, 5), sheet, 1.6),
        ("High Score (Unseen)", lambda: _scroll(win.apply_panel._sheet, 5), sheet, 1.6),
        ("High Score (Unseen)", lambda: _scroll(win.apply_panel._sheet, 5), sheet, 1.6),
        ("High Score (Unseen)", lambda: _scroll(win.apply_panel._sheet, 5),
         "The browser agent fills a form from this, and stops before Submit", 3.0),
        ("All Jobs", lambda: (win._close_apply_panel(), _pick(win.all_tab, 0)),
         every, 3.0),
        ("All Jobs", lambda: _pick(win.all_tab, 4), every, 1.8),
        ("All Jobs", lambda: _pick(win.all_tab, 9), every, 2.2),
        ("Tracker", lambda: _pick(win.tracker_tab, 0),
         "Tracker - application status, with follow-ups flagged when due", 4.2),
        ("Tracker", lambda: _pick(win.tracker_tab, 2), statuses, 1.8),
        ("Tracker", lambda: _pick(win.tracker_tab, 3), statuses, 1.8),
        ("Tracker", lambda: _pick(win.tracker_tab, 4), statuses, 2.4),
        ("Auto-apply", lambda: None, queue, 3.0),
        ("Auto-apply", lambda: _pick(win.apply_queue_panel, 3), queue, 1.8),
        ("Auto-apply", lambda: _pick(win.apply_queue_panel, 4), queue, 1.8),
        ("Auto-apply", lambda: _pick(win.apply_queue_panel, 6), queue, 2.2),
        ("Stats", lambda: None, runs, 3.4),
        ("Stats", lambda: _pick(win.stats_tab, 3), runs, 1.8),
        ("Stats", lambda: _pick(win.stats_tab, 6), runs, 2.2),
        ("Resume Data", lambda: None, atoms, 3.4),
        ("Resume Data", lambda: _scroll(win.resume_data_tab.scroll, 6), atoms, 1.6),
        ("Resume Data", lambda: _scroll(win.resume_data_tab.scroll, 6), atoms, 1.6),
        ("Resume Data", lambda: _scroll(win.resume_data_tab.scroll, 6), atoms, 1.6),
        ("Resume Data", lambda: _scroll(win.resume_data_tab.scroll, 6), atoms, 2.2),
        # Apply Answers gets ONE hold: its sixteen rows fit the window, so a
        # scroll beat there produced a byte-identical frame and Pillow folded it
        # into the hold before it -- a pause dressed up as motion.
        ("Apply Answers", lambda: None, answers, 3.4),
        # Scrolling Settings is what shows that the ten sections are all there;
        # Credentials and Connection & paths stay FOLDED throughout (only Engine
        # is expanded), so no secret field is ever on screen.
        ("Settings", lambda: None, knobs, 3.2),
        ("Settings", lambda: _scroll_frac(win.settings_tab._scroll, 1 / 3), knobs, 1.6),
        ("Settings", lambda: _scroll_frac(win.settings_tab._scroll, 2 / 3), knobs, 1.6),
        ("Settings", lambda: _scroll_frac(win.settings_tab._scroll, 1.0), knobs, 2.8),
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


def render_scenes(tmp_dir: Path) -> tuple[list[Image.Image], list[float], list[str]]:
    """Drive the real MainWindow offscreen through `scenes()` and return one
    captioned frame per storyboard step, its seconds-on-screen, and its caption.

    The captions come back because they are what tells a consumer where the SCENE
    boundaries are: a step repeating the caption before it is a beat inside one
    scene (a keystroke, a scroll) and wants a hard cut, while a changed caption is
    a real cut and wants a crossfade. `build_demo_media` re-times on exactly that.

    Shared with `scripts/build_demo_media.py`, which re-times the same
    storyboard into `docs/demo.gif`, so the video and the GIF can never drift
    into telling two different stories. Everything here runs against
    `ui_screenshots`' synthetic fixtures: no network, no real data, no API call.
    """
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
    uis._freeze_auto_reload(win)
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
    uis._expand_settings_section(win)
    # The finished tailor run the tour opens its apply sheet from. Built once, up
    # front, so the scene that shows it is a panel opening rather than a wait.
    win._demo_tailored_folder = uis._tailored_folder(tmp_dir)
    app.processEvents()

    holds: list[Image.Image] = []
    secs: list[float] = []
    captions: list[str] = []
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
        captions.append(caption)
    return holds, secs, captions


def main() -> int:
    tmp = tempfile.TemporaryDirectory(prefix="walkthrough_")
    tmp_dir = Path(tmp.name)
    frames_dir = tmp_dir / "frames"
    frames_dir.mkdir()

    holds, secs, captions = render_scenes(tmp_dir)
    if not holds:
        print("no frames captured")
        return 1

    # Write holds + crossfade frames as PNGs, then let ffmpeg time them. A fade is
    # written only where the caption changes; a beat inside a scene (a keystroke, a
    # scroll) hard-cuts, because dissolving one keystroke into the next is a blur,
    # not a transition.
    entries: list[tuple[Path, float]] = []
    fade_dur = 1.0 / FPS
    for i, (hold, dur) in enumerate(zip(holds, secs)):
        p = frames_dir / f"hold_{i:03d}.png"
        hold.save(p)
        entries.append((p, dur))
        if i + 1 < len(holds) and captions[i + 1] != captions[i]:
            for k in range(1, FADE_FRAMES + 1):
                fp = frames_dir / f"fade_{i:03d}_{k:02d}.png"
                Image.blend(hold, holds[i + 1], k / (FADE_FRAMES + 1)).save(fp)
                entries.append((fp, fade_dur))

    DOCS.mkdir(exist_ok=True)
    _encode(entries, OUT)
    total = sum(d for _, d in entries)
    mb = OUT.stat().st_size / 1e6
    print(f"{OUT} -> {len(set(captions))} scenes, {len(holds)} frames, {total:.0f}s, "
          f"{mb:.1f} MB, {holds[0].width}x{holds[0].height}")
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
