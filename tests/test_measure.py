"""Width-aware line measurement (resume_tailor/measure.py) + its use in the trim.

The old flat char-count line estimate (len(text)/MAX_LINE_CHARS) couldn't tell a wide
word ('cross-encoder') from a narrow one, so a bullet at the 2-line boundary could wrap
to 3 lines unnoticed. measure.line_count models the real render (per-char advance widths
+ greedy word wrap against the calibrated body-column capacity). The two REAL bullets
below are ground truth: in the actual compiled PDF the first renders on 2 lines and the
second (shorter in chars, but with wide words) on 3 — calibrated/validated against it.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

from resume_tailor import compose, config, measure  # noqa: E402
from resume_tailor import run as rt_run  # noqa: E402

# Verbatim from a real compiled resume (Reducto tailor); known printed line counts.
TWO_LINE = ("Systematized a resumable pipeline to ingest and index 34,060 wiki articles into "
            "289,196 chunks via the MediaWiki API, designing a multi-tag game-membership schema "
            "with boolean metadata flags in ChromaDB to prevent character data from leaking "
            "across filters.")
THREE_LINE = ("Synthesized a hybrid retriever fusing ChromaDB vector search with SQLite FTS5 BM25 "
              "via Reciprocal Rank Fusion and cross-encoder reranking, utilizing a 40-question "
              "evaluation harness to resolve retrieval failures and eliminate cross-game metadata "
              "leakage.")


# --- text_width ----------------------------------------------------------------

def test_text_width_orders_wide_above_narrow():
    assert measure.text_width("WWWW") > measure.text_width("iiii")
    assert measure.text_width("mmm") > measure.text_width("lll")
    assert measure.text_width("") == 0


# --- line_count ----------------------------------------------------------------

def test_line_count_short_text_is_one_line():
    assert measure.line_count("Built a small pipeline") == 1


def test_line_count_empty_is_one():
    assert measure.line_count("") == 1
    assert measure.line_count("   ") == 1


def test_line_count_matches_real_two_line_bullet():
    assert measure.line_count(TWO_LINE) == 2


def test_line_count_matches_real_three_line_bullet():
    # The char heuristic counted this as 2 (254 chars < 2*130); the real render is 3.
    assert measure.line_count(THREE_LINE) == 3
    # And it is SHORTER in characters than the 2-line bullet — proving char count lies.
    assert len(THREE_LINE) < len(TWO_LINE)


def test_line_count_respects_capacity_param():
    half = measure.BODY_LINE_CAPACITY // 2
    assert measure.line_count(TWO_LINE, capacity=half) > measure.line_count(TWO_LINE)


# --- width-aware trim (run._fit_to_lines / _trim_to_caps) ----------------------

def test_fit_to_lines_trims_overflowing_bullet_to_target():
    out = rt_run._fit_to_lines(THREE_LINE, 2)
    assert measure.line_count(out) <= 2          # genuinely fits 2 printed lines now
    assert out.startswith("Synthesized")         # front-loaded content kept
    assert len(out) < len(THREE_LINE)            # only the overflow tail was trimmed


def test_fit_to_lines_leaves_a_fitting_bullet_untouched():
    assert rt_run._fit_to_lines(TWO_LINE, 2) == TWO_LINE


def test_trim_to_caps_uses_width_not_char_count(monkeypatch):
    # A project bullet with a wide-word overflow that the old char cap (254 < 2*130) missed.
    monkeypatch.setattr(config, "_config_json", lambda: {})   # no project_layout -> default 2 lines
    sel = {"experience": [], "leadership": [],
           "projects": [{"name": "XenoRAG", "groups": [["a1"]]}]}
    gk = compose._gkey(["a1"])
    bullets = {gk: THREE_LINE}
    rt_run._trim_to_caps(sel, bullets)
    assert measure.line_count(bullets[gk]) <= 2
    assert len(bullets[gk]) < len(THREE_LINE)
