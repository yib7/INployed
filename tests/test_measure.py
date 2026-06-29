"""Width-aware line measurement (resume_tailor/measure.py) + its use in the trim.

The old flat char-count line estimate (len(text)/MAX_LINE_CHARS) couldn't tell a wide
word ('cross-encoder') from a narrow one, so a bullet at the 2-line boundary could wrap
to 3 lines unnoticed. measure.line_count models the real render (per-char advance widths
+ greedy word wrap against the calibrated body-column capacity). The two REAL bullets
below are ground truth: in the actual compiled PDF the first renders on 2 lines and the
second (shorter in chars, but with wide words) on 3 — calibrated/validated against it.
"""
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

from resume_tailor import assets, compose, config, measure  # noqa: E402
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


# A real over-length Work bullet whose trailing quantity the model spelled out as a word
# range ("took 1 to 2 weeks per cycle"). Trimming to 2 lines used to chop it to
# "...that previously took 1." — a dangling bare number — because _strip_dangling removed
# only the innermost connective ("to 2" -> "took 1") and stopped.
DANGLING_NUM_BULLET = (
    "Validated the new extraction model by backtesting against production data to achieve "
    ">=95% accuracy, implementing post-processing statistical checks and historical "
    "cross-references to minimize manual-review bottlenecks that previously took 1 to 2 weeks per cycle"
)


def test_fit_to_lines_never_ends_on_dangling_number():
    out = rt_run._fit_to_lines(DANGLING_NUM_BULLET, 2)
    assert measure.line_count(out) <= 2
    assert not re.search(r"\d+\s*$", out)            # no trailing bare number ("...took 1")
    assert not out.rstrip(".").endswith("took 1")
    assert out.split()[-1].rstrip(".") == "bottlenecks"   # whole chopped clause dropped cleanly


def test_strip_dangling_drops_chopped_quantity_clause():
    text = "to minimize manual-review bottlenecks that previously took 1 to 2"
    assert rt_run._strip_dangling(text) == "to minimize manual-review bottlenecks"


def test_strip_dangling_keeps_unit_bearing_trailing_number():
    # The new bare-number rule must NOT fire on a number that carries a unit/noun: a trailing
    # '40,000+ users' or '95%' is a complete metric, not a chopped quantity, so it stays.
    keep = "Processed high-volume transactions for over 40,000+ users"
    assert rt_run._strip_dangling(keep) == keep
    keep2 = "Maintained checkout speed ranking among the top 95%"
    assert rt_run._strip_dangling(keep2) == keep2


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


# --- parenthesized (comma-bearing) skill tokens --------------------------------
# A merged token like "LLM APIs (Gemini, OpenAI, Claude)" carries internal commas;
# splitting a skills line on every comma shatters it into 3 fragments, which then
# count as 3 toward the best-N target (so a 10-target line stops at ~8 visual items)
# and can be cut mid-parenthesis. The skills splitter must be parenthesis-aware.

def test_split_skill_tokens_keeps_parenthesized_comma_token():
    toks = compose._split_skill_tokens("AWS, LLM APIs (Gemini, OpenAI, Claude), Redis")
    assert toks == ["AWS", "LLM APIs (Gemini, OpenAI, Claude)", "Redis"]


def test_split_skill_tokens_plain_line_unchanged():
    assert compose._split_skill_tokens("Python, SQL, R") == ["Python", "SQL", "R"]


def test_complete_to_count_counts_parenthesized_token_as_one():
    # The "(Gemini, OpenAI, Claude)" token counts as ONE item, so the line completes to
    # the full target from the pool instead of stopping early on its shattered commas.
    pool = ["AWS", "S3", "Lambda", "EC2", "RDS"]
    picked = compose._complete_to_count(
        "AWS, LLM APIs (Gemini, OpenAI, Claude), S3", pool, 5)
    assert len(picked) == 5                                 # 5 VISUAL items, not 3
    assert "LLM APIs (Gemini, OpenAI, Claude)" in picked    # token kept intact
    assert "Lambda" in picked and "EC2" in picked           # completed from the pool


def test_cap_items_never_emits_unbalanced_parens(monkeypatch):
    # With a capacity that would fit only a FRAGMENT of the paren token, _cap_items must
    # drop the whole token, never cut it to "...LLM APIs (Gemini" (an unclosed paren).
    line = "AWS, LLM APIs (Gemini, OpenAI, Claude), Redis"
    cap = measure.skill_line_width("Developer Tools", "AWS, LLM APIs (Gemini, OpenAI")
    monkeypatch.setattr(compose.measure, "SKILL_LINE_CAPACITY", cap)
    out = compose._cap_items("Developer Tools", line)
    assert out.count("(") == out.count(")")                 # never cut mid-parenthetical


def test_master_skill_pools_have_no_comma_shattered_tokens():
    # Guard the master data: a parenthesized skill must be ONE YAML list entry, not split
    # by flow-sequence commas into "LLM APIs (Gemini" / "OpenAI" / "Claude)".
    skills = assets.load_master().get("skills", {})
    for pool, items in skills.items():
        for it in items:
            assert it.count("(") == it.count(")"), f"{pool}: unbalanced parens in {it!r}"
