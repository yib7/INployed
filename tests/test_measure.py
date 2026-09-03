"""Width-aware line measurement (resume_tailor/measure.py) + its use in the trim.

The old flat char-count line estimate (len(text)/<chars per line>) couldn't tell a wide
word ('cross-encoder') from a narrow one, so a bullet at the 2-line boundary could wrap
to 3 lines unnoticed. measure.line_count models the real render (per-char advance widths
+ greedy word wrap against the calibrated body-column capacity). The two REAL bullets
below are ground truth: in the actual compiled PDF the first renders on 2 lines and the
second (shorter in chars, but with wide words) on 3 — calibrated/validated against it.
"""
import importlib
import os
import re
import sys
from math import ceil
from pathlib import Path

import pytest

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


def test_fill_floor_width_single_line_is_half():
    # The underfull-fill TRIGGER only rescues a line below 50% full (was 90%).
    from math import ceil
    assert measure.fill_floor_width(1) == ceil(0.50 * measure.BODY_LINE_CAPACITY)


def test_fill_floor_width_multiline_reaches_into_last_line():
    from math import ceil
    # A 2-line bullet only triggers fill below 50% into its second line: (1 + 0.50) * capacity.
    assert measure.fill_floor_width(2) == ceil(1.50 * measure.BODY_LINE_CAPACITY)


def test_is_underfull_false_for_sixty_percent_line():
    # A single-line bullet filling ~55-60% of its line is NO LONGER rescued (was, under 0.90):
    # some white space is fine to keep it readable; only sub-50% lines trigger the fill.
    from math import ceil
    cap = measure.BODY_LINE_CAPACITY
    text = ""
    while measure.text_width(text) < ceil(0.55 * cap):
        text += "wide "
    assert ceil(0.50 * cap) <= measure.text_width(text) < ceil(0.90 * cap)
    assert measure.is_underfull(text, 1) is False        # would have been True at the old 0.90 floor


def test_length_hint_aim_unchanged_when_trigger_relaxed():
    # The TRIGGER drops to 50%, but the rephrase AIM stays 0.90/0.75 — intentionally decoupled
    # (we still ask the model to fill the line; we just stop folding in a spare atom above 50%).
    from math import ceil
    assert measure.FULL_LINE_FILL == 0.90 and measure.LAST_LINE_FILL == 0.75   # aim constants
    assert measure.UNDERFULL_FILL == 0.50                                      # new trigger constant
    assert f"{ceil(0.90 * measure.char_budget(1))}-" in compose._length_hint(1)


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


def test_cap_items_best_fit_skips_overwide_keeps_later_short_token(monkeypatch):
    # Best-fit packing: a wide MIDDLE token that overflows must not stop the line — a
    # shorter token after it still gets its slot, instead of wasting the rest of the line.
    line = "AWS, Supercalifragilistic Framework Name, Git"
    cap = measure.skill_line_width("Developer Tools", "AWS, Git")
    monkeypatch.setattr(compose.measure, "SKILL_LINE_CAPACITY", cap)
    out = compose._cap_items("Developer Tools", line)
    assert out == "AWS, Git"          # middle token skipped; trailing short token kept
    # (old first-overflow-break behavior would have stopped at "AWS")


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
    # (Pool carries the token's members so it ANCHORS -- SP7 requires every named member
    # to be pool-backed; this test pins the not-shattered COUNTING, not the anchor gate.)
    pool = ["AWS", "S3", "Lambda", "EC2", "RDS", "Gemini", "OpenAI", "Claude"]
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


# --- SP3: the character ceiling the PROMPT states vs. real measured capacity ----
#
# compose._length_hint used to build its ceiling as `target_lines * config.MAX_LINE_CHARS`
# (a flat 130). Greedy word wrap makes real capacity SUBLINEAR in the line count, so that
# overshot what fits at every n > 1 — the model was invited past the line, the bullet wrapped,
# and the deterministic trim had to cut it back. measure.char_budget replaces it. These tests
# are built from real measure.line_count calls rather than from a table of numbers, so they
# pin the PROPERTY (the stated cap fits) and not the values it happened to have when written.

# Representative bullet prose, each entry longer than a 3-line budget so the assertions
# actually bite (a bullet shorter than the cap would satisfy them trivially). First entry:
# the two ground-truth bullets above, joined. The rest span the density range the engine
# sees — wide technical nouns and capitalized product names through to plain narrow prose.
_BUDGET_CORPUS = (
    TWO_LINE + " " + THREE_LINE,
    "Rebuilt the nightly ETL pipeline in Python against PostgreSQL and cut batch runtime "
    "42%, keeping the ingestion service green across the whole summer while the warehouse "
    "kept taking on new raw event volume, and wrote the runbook that the on-call rotation "
    "followed all season without a single paging escalation from the data platform team, "
    "then handed the schedule to a rotating owner in each of the four regional squads.",
    "Instrumented the retrieval service with OpenTelemetry traces and Prometheus metrics, "
    "cutting p99 query latency from 1.8 s to 320 ms by replacing a per-request embedding "
    "call with a warmed cross-encoder cache, and documented the rollout in a runbook the "
    "on-call rotation used to resolve the next two incidents without escalating further, "
    "which took the service off the quarterly reliability review it had been stuck on.",
    "Led a team of nine students to a regional finals placement and ran weekly workshops "
    "for forty new members, growing club turnout through the year and handing the incoming "
    "officers a written curriculum that covered wiring, debouncing and serial debugging so "
    "the program kept running after the founding cohort graduated in the spring of a year, "
    "with a parts budget the treasurer could defend line by line at the funding hearing.",
    "Migrated 47 microservices from a hand-rolled Kubernetes manifest tree to Helm charts "
    "with environment overlays, removing 6,200 lines of duplicated YAML and letting a new "
    "engineer ship a service to staging on their first day instead of the week the older "
    "onboarding checklist budgeted for it under the previous deployment arrangement here, "
    "and cut the release captain's manual steps from nineteen to three in every namespace.",
)


def _longest_fitting_prefix(text: str, target_lines: int) -> int:
    """The longest character prefix of `text` that still renders within `target_lines`
    printed lines — the REAL capacity, found with measure itself (line_count is monotonic
    in length) rather than assumed from a table."""
    lo, hi, best = 0, len(text), 0
    while lo <= hi:
        mid = (lo + hi) // 2
        if measure.line_count(text[:mid]) <= target_lines:
            best, lo = mid, mid + 1
        else:
            hi = mid - 1
    return best


@pytest.mark.parametrize("target", [1, 2, 3])
def test_char_budget_never_exceeds_real_capacity(target):
    """The ceiling handed to the model must FIT. Written to the cap, every representative
    bullet has to render within its line target — otherwise the prompt is asking for a
    bullet the page cannot hold and the trim inherits the mess."""
    budget = measure.char_budget(target)
    for text in _BUDGET_CORPUS:
        assert len(text) > budget, "corpus entry too short to exercise the budget"
        assert measure.line_count(text[:budget]) <= target, (
            f"{budget}-char cap for {target} line(s) overflows: {text[:budget]!r}")


@pytest.mark.parametrize("target", [1, 2, 3])
def test_char_budget_is_not_needlessly_conservative(target):
    """The other direction: a cap that fits by being tiny would starve every bullet. It
    must stay within 10% of what actually fits."""
    budget = measure.char_budget(target)
    for text in _BUDGET_CORPUS:
        real = _longest_fitting_prefix(text, target)
        assert budget >= 0.90 * real, f"cap {budget} wastes too much of a real {real}"


def test_char_budget_is_sublinear_in_the_line_count():
    """The whole point of the helper: n lines hold LESS than n one-line bullets, because
    greedy wrap drops the word that will not fit onto the next line and leaves the line
    before it short. A future 'simplification' to n * chars_per_line fails here."""
    b1, b2, b3 = (measure.char_budget(n) for n in (1, 2, 3))
    assert b1 < b2 < b3            # more lines still buys more characters
    assert b2 < 2 * b1             # ... but fewer than a flat multiply promises
    assert b3 < 3 * b1


def test_char_budget_tracks_the_calibrated_capacity():
    """Derived from BODY_LINE_CAPACITY, not hardcoded — so recalibrating the column for a
    different template font/geometry moves the prompt's ceiling with it."""
    full = measure.char_budget(2)
    assert measure.char_budget(2, capacity=measure.BODY_LINE_CAPACITY) == full
    half = measure.char_budget(2, capacity=measure.BODY_LINE_CAPACITY // 2)
    assert half == pytest.approx(full / 2, abs=1)


def test_char_budget_floors_at_one_line():
    # A zero/negative line target from a bad config must not produce a zero-char cap.
    assert measure.char_budget(0) == measure.char_budget(1)
    assert measure.char_budget(-3) == measure.char_budget(1)
    assert measure.char_budget(1) > 0


def test_length_hint_ceiling_is_the_measured_budget():
    """_length_hint states measure.char_budget, and its floor stays below it."""
    for n in (1, 2, 3):
        cap = measure.char_budget(n)
        hint = compose._length_hint(n)
        assert f"-{cap} characters" in hint
        assert f"never exceed {cap}" in hint
        floor = int(re.search(r"\((\d+)-", hint).group(1))
        assert 0 < floor < cap


# --- SP3: the prompt's fill percentages come from the constants -----------------
def _rephrase_system_prompt(monkeypatch) -> str:
    """Render rephrase's system prompt against a stubbed `call` (nothing leaves the
    process). Returns the system prompt the model would have been sent."""
    captured = {}

    def fake_call(system, user, tier, **kw):
        captured["system"] = system
        return {"bullets": [{"gkey": "a1", "text": "Built A."}]}

    monkeypatch.setattr(compose, "_atom_payload", lambda a: {"what": f"did {a}"})
    monkeypatch.setattr(compose.assets, "example_text", lambda: "exemplar voice")
    monkeypatch.setattr(compose, "call", fake_call)
    sel = {"experience": [{"name": "Globex", "groups": [["a1"]]}],
           "projects": [], "leadership": []}
    compose.rephrase("jd", "Eng", sel)
    return captured["system"]


def test_prompt_space_percentages_state_the_defaults(monkeypatch):
    system = _rephrase_system_prompt(monkeypatch)
    assert "~90% of it" in system and "~75% full" in system


def test_prompt_space_percentages_track_the_fill_constants(monkeypatch):
    """The prompt spelled '~90%' / '~75%' out as literals while measure held the same two
    numbers, so retuning a constant moved the FLOOR without changing what the model was
    told. Retune both and the prompt has to follow."""
    monkeypatch.setattr(measure, "FULL_LINE_FILL", 0.80)
    monkeypatch.setattr(measure, "LAST_LINE_FILL", 0.55)
    system = _rephrase_system_prompt(monkeypatch)
    assert "~80% of it" in system and "~55% full" in system
    assert "~90%" not in system and "~75%" not in system


# --- SP3: env overrides for the three fill fractions ----------------------------
_FILL_ENV = ("RESUME_TAILOR_FULL_LINE_FILL", "RESUME_TAILOR_LAST_LINE_FILL",
             "RESUME_TAILOR_UNDERFULL_FILL")
_FILL_DEFAULTS = (0.90, 0.75, 0.50)


@pytest.fixture()
def reload_measure():
    """Re-import measure under a chosen environment so its import-time constants are
    re-read. importlib.reload mutates the module object in place, so compose/run keep
    seeing the same object; the teardown restores the documented defaults."""
    def _load(**env):
        for name in _FILL_ENV:
            os.environ.pop(name, None)
        os.environ.update(env)
        return importlib.reload(measure)

    yield _load
    for name in _FILL_ENV:
        os.environ.pop(name, None)
    importlib.reload(measure)


def _fills(mod):
    return (mod.FULL_LINE_FILL, mod.LAST_LINE_FILL, mod.UNDERFULL_FILL)


def test_fill_fraction_env_overrides_are_honoured(reload_measure):
    m = reload_measure(RESUME_TAILOR_FULL_LINE_FILL="0.75",
                       RESUME_TAILOR_LAST_LINE_FILL="0.5",
                       RESUME_TAILOR_UNDERFULL_FILL="0.35")
    assert _fills(m) == (0.75, 0.5, 0.35)


def test_fill_fractions_default_when_unset(reload_measure):
    assert _fills(reload_measure()) == _FILL_DEFAULTS


@pytest.mark.parametrize("bad", ["", "   ", "ninety", "0.9x", "None", "90%"])
def test_fill_fractions_default_on_garbage(reload_measure, bad):
    m = reload_measure(RESUME_TAILOR_FULL_LINE_FILL=bad,
                       RESUME_TAILOR_LAST_LINE_FILL=bad,
                       RESUME_TAILOR_UNDERFULL_FILL=bad)
    assert _fills(m) == _FILL_DEFAULTS


@pytest.mark.parametrize("bad", ["0", "0.0", "-0.5", "1.5", "2.0", "100", "nan", "inf"])
def test_fill_fractions_default_when_out_of_range(reload_measure, bad):
    # A 0 ("every bullet is full enough") or a 2.0 ("none ever is") must never reach the
    # engine: it silently disables the fill rescue or fires it on every bullet.
    m = reload_measure(RESUME_TAILOR_FULL_LINE_FILL=bad,
                       RESUME_TAILOR_LAST_LINE_FILL=bad,
                       RESUME_TAILOR_UNDERFULL_FILL=bad)
    assert _fills(m) == _FILL_DEFAULTS


def test_underfull_env_override_reaches_the_trigger(reload_measure):
    """Wired, not merely stored: the override moves the real rescue threshold."""
    m = reload_measure(RESUME_TAILOR_UNDERFULL_FILL="0.30")
    assert m.fill_floor_width(1) == ceil(0.30 * m.BODY_LINE_CAPACITY)
    assert m.fill_floor_width(2) == ceil(1.30 * m.BODY_LINE_CAPACITY)
