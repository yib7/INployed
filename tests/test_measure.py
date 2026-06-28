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


# --- underfull detection (fill_floor_width / is_underfull) ---------------------

def test_fill_floor_width_monotonic_in_target():
    assert measure.fill_floor_width(1) < measure.fill_floor_width(2) < measure.fill_floor_width(3)


def test_fill_floor_width_single_line_is_ninety_percent():
    from math import ceil
    assert measure.fill_floor_width(1) == ceil(0.90 * measure.BODY_LINE_CAPACITY)


def test_fill_floor_width_multiline_reaches_into_last_line():
    from math import ceil
    # A 2-line bullet must reach >=75% into its second line: (1 + 0.75) * capacity.
    assert measure.fill_floor_width(2) == ceil(1.75 * measure.BODY_LINE_CAPACITY)


def test_is_underfull_true_for_stubby_bullet():
    assert measure.is_underfull("Built a small tool", 2) is True


def test_is_underfull_false_once_width_passes_floor():
    # A wide string whose advance width exceeds the 1-line floor is not underfull at target 1.
    text = "wide " * 80
    assert measure.text_width(text) >= measure.fill_floor_width(1)
    assert measure.is_underfull(text, 1) is False


def test_is_underfull_keys_off_fill_floor_width():
    target = 2
    big = "wide " * 80
    assert measure.text_width(big) >= measure.fill_floor_width(target)
    assert measure.is_underfull(big, target) is False
    assert measure.is_underfull("x", target) is True


# --- skills line width (bold label + items) ------------------------------------
# Verbatim from the same real PDF; this Developer Tools line rendered on ONE line.
DT_ITEMS = "LLM APIs (Gemini/OpenAI/Claude), AWS, S3, Docker, Kafka, PostgreSQL, Redis, ChromaDB"


def test_text_width_bold_is_wider_than_regular():
    assert measure.text_width("Developer Tools", bold=True) > measure.text_width("Developer Tools")


def test_skill_line_capacity_defaults_to_body_column():
    # Skills share the body text column AND font size (validated against the real PDF),
    # so by default the one-line capacity is identical.
    assert measure.SKILL_LINE_CAPACITY == measure.BODY_LINE_CAPACITY


def test_real_skill_line_fits_one_line():
    assert measure.skill_line_width("Developer Tools", DT_ITEMS) <= measure.SKILL_LINE_CAPACITY


def test_overlong_skill_line_overflows():
    too_long = DT_ITEMS + ", " + DT_ITEMS                    # clearly more than one line
    assert measure.skill_line_width("Developer Tools", too_long) > measure.SKILL_LINE_CAPACITY


def test_cap_items_drops_tail_to_fit_one_line_by_width(monkeypatch):
    # Capacity set to exactly the width of "Languages: Python, SQL" -> the next item overflows.
    cap = measure.skill_line_width("Languages", "Python, SQL")
    monkeypatch.setattr(compose.measure, "SKILL_LINE_CAPACITY", cap)
    out = compose._cap_items("Languages", "Python, SQL, JavaScript, TypeScript")
    assert out == "Python, SQL"


def test_cap_items_keeps_at_least_the_first_token(monkeypatch):
    # Even a single wide token that overflows is kept (a line is never emptied).
    monkeypatch.setattr(compose.measure, "SKILL_LINE_CAPACITY", 1)
    assert compose._cap_items("Languages", "Python, SQL") == "Python"
