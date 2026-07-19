"""Dashboard thread hygiene (audit P1-5/P1-6/P2-5/P2-11/P2-19/P2-24/P2-30):
routine actions must never do synchronous Drive/master reads on the UI thread,
and assorted Qt fixes ride along.
"""
import textwrap
from unittest.mock import MagicMock

import pandas as pd
from PySide6 import QtCore

from qt import main_window as mw
from qt.detail_card import JobDetailCard
from qt.main_window import MainWindow
from qt.resume_data_tab import ResumeDataEditor


def _fake_registry():
    reg = MagicMock()
    reg.resume_paths.return_value = {}
    reg.status_rows.return_value = []
    return reg


def _win(qtbot):
    w = MainWindow(csv_paths=[], registry=_fake_registry())
    qtbot.addWidget(w)
    return w


# ── P1-5: no synchronous stats read on the UI thread ─────────────────────────

def test_apply_df_views_does_not_read_stats_from_disk(qtbot, monkeypatch):
    """_refresh_stats must render from the cached frame the off-thread loader
    produced — a mark-seen/undo/delete repaint never touches run_stats.csv."""
    w = _win(qtbot)
    calls = []
    monkeypatch.setattr(mw.pd, "read_csv",
                        lambda *a, **k: calls.append(a) or pd.DataFrame())
    w._apply_df_views()
    assert calls == []


def test_load_frames_carries_the_stats_frame(qtbot, monkeypatch, tmp_path):
    """The off-thread loader reads run_stats.csv alongside the job sources, so the
    UI-thread half only formats it."""
    stats = tmp_path / "run_stats.csv"
    stats.write_text("timestamp,total_scraped\n2026-07-18T08:00:00,5\n",
                     encoding="utf-8")
    master = tmp_path / "linkedin_jobs_master.csv.gz"
    pd.DataFrame({"job_posting_id": ["1"], "job_title": ["T"],
                  "score": ["5"]}).to_csv(master, index=False, compression="gzip")
    monkeypatch.setattr(mw, "gdrive_root_dir", lambda paths: tmp_path)
    monkeypatch.setattr(mw.settings, "load", lambda *a, **k: {})
    w = MainWindow(csv_paths=[master], registry=_fake_registry())
    qtbot.addWidget(w)
    loaded = w._load_frames()
    assert getattr(loaded, "stats", None) is not None
    assert not loaded.stats.empty and int(loaded.stats.iloc[0]["total_scraped"]) == 5


# ── P1-6: routine actions reload asynchronously ──────────────────────────────

def test_block_company_reloads_async(qtbot, monkeypatch, tmp_path):
    w = _win(qtbot)
    monkeypatch.setattr(mw.jobsdata, "append_to_blocklist", lambda *a: None)
    called = []
    monkeypatch.setattr(w, "reload_data_async", lambda: called.append(True))
    monkeypatch.setattr(w, "reload_data",
                        lambda: (_ for _ in ()).throw(AssertionError("sync reload")))
    w._block_company("Acme")
    assert called == [True]


def test_settings_saved_reloads_async(qtbot, monkeypatch):
    w = _win(qtbot)
    called = []
    monkeypatch.setattr(w, "reload_data_async", lambda: called.append(True))
    monkeypatch.setattr(w, "reload_data",
                        lambda: (_ for _ in ()).throw(AssertionError("sync reload")))
    w._on_settings_saved()
    assert called == [True]


# ── P2-24: per-selection disk stats are cached ───────────────────────────────

def test_apply_ready_is_cached_between_calls(qtbot, monkeypatch):
    w = _win(qtbot)
    probes = []

    def fake_resume_path(jid):
        probes.append(jid)
        return None

    w.registry.resume_path = fake_resume_path
    w._apply_ready("j1")
    w._apply_ready("j1")          # second call within the TTL → served from cache
    assert probes == ["j1"]


def test_disk_cache_cleared_on_frame_apply(qtbot, monkeypatch):
    """A reload invalidates every cached disk probe (a fresh reload may then
    re-probe and repopulate — the STALE entry must be gone)."""
    w = _win(qtbot)
    w.registry.resume_path = lambda jid: None
    w._apply_ready("j1")
    assert ("apply_ready", "j1") in w._disk_cache
    w._apply_frames(mw.LoadedFrames(pd.DataFrame(), {}, None, 36))
    assert ("apply_ready", "j1") not in w._disk_cache


# ── P2-11: a failed tailor-thread spawn surfaces in the status bar ───────────

def test_start_tailor_spawn_failure_does_not_reraise(qtbot, monkeypatch):
    w = _win(qtbot)
    monkeypatch.setattr(mw.workers, "run_async",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no threads")))
    monkeypatch.setattr(w, "_apply_auth_env", lambda: None)
    out = w._start_tailor([{"job_posting_id": "1"}], {})
    assert out is False                       # reported as not-launched, no raise
    assert not w._tailoring                   # guard cleared
    assert "no threads" in w.statusBar().currentMessage()


# ── P2-19: the JD snippet renders as plain text ──────────────────────────────

def test_detail_card_jd_is_plain_text(qtbot):
    card = JobDetailCard()
    qtbot.addWidget(card)
    assert card.desc_label.textFormat() == QtCore.Qt.TextFormat.PlainText
    card.set_fields({"title": "T", "company": "C",
                     "jd": "before <b>bold</b> <img src='x'> after"}, jid="1")
    assert "<b>bold</b>" in card.desc_label.text()   # verbatim, not rich-text


# ── P2-5: duplicate entry names keep both verbatim editors ───────────────────

_DUP_YAML = textwrap.dedent("""\
    basics:
      name: Jane Doe
      email: jane@example.com
    experience:
      - org: Example Corp
        title: Intern
        dates: "2024-06 / 2024-08"
        achievements:
          - id: a1
            what: did a thing
            angles: [backend]
      - org: Example Corp
        title: Analyst
        dates: "2025-06 / 2025-08"
        achievements:
          - id: a2
            what: did more
            angles: [data]
""")


def test_duplicate_entry_names_keep_both_verbatim_editors(qtbot, tmp_path):
    p = tmp_path / "master_experience.yaml"
    p.write_text(_DUP_YAML, encoding="utf-8")
    ed = ResumeDataEditor(master_path=p)
    qtbot.addWidget(ed)
    # Two same-named entries must not clobber each other's editor widgets.
    dup_keys = [k for k in ed._verbatim_edits if not isinstance(k, str)]
    assert len(ed._verbatim_edits) == 2 or len(dup_keys) == 2


# ── P2-30: scale docstring matches code; push-state has a public slot ────────

def test_apply_scale_docstring_matches_clamp():
    doc = MainWindow._apply_scale.__doc__ or ""
    assert "75" in doc and "150" in doc and "[50, 200]" not in doc


def test_resume_data_tab_exposes_public_push_state_slot(qtbot, master_tmp):
    ed = ResumeDataEditor(master_path=master_tmp)
    qtbot.addWidget(ed)
    assert callable(getattr(ed, "refresh_push_state", None))
