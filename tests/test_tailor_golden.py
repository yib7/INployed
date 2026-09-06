"""Golden-output oracle for the résumé-tailor engine.

The file was born in cycle 9 (design spec:
``docs/superpowers/specs/2026-09-01-tailor-legibility-design.md``), a pass over
``local/resume_tailor/`` that moved code without changing what the engine produces:
SP2 turned ``run.tailor()``'s hand-written stage sequence into a declarative pass
pipeline, SP4 split ``compose.py`` into ``selection.py`` + ``skills.py``, SP5 deleted
the parts of the tree that were documented but never real. All three had to leave the
output byte-identical, and "byte-identical" is not something a reviewer can eyeball
across a 1,571-line module move. So it was pinned here instead.

The test drives the whole bullet pipeline in the exact order ``run.tailor()`` runs it —
``select`` -> ``inject_verbatim`` -> ``lead_with_overview`` -> ``block_briefs`` ->
``rephrase`` -> grounding gate -> ``dedupe_leading_verbs`` -> gate -> verbatim merge ->
``_trim_to_caps`` -> ``fill_underfull`` -> retrim -> gate -> ``enforce_style`` -> retrim ->
gate -> ``compress_skills`` -> ``methods_line`` -> ``render.render`` — and asserts the EXACT final
``bullets`` dict and the EXACT rendered ``.tex``. The expected values below are literals:
they were produced by running this pipeline once and pasting what came out, so the test
compares the engine against a frozen recording rather than against itself.

**The contract changed at cycle 10.** Cycle 9 was a pure refactor, so any diff here was
a bug and the rule was "do not update the golden to make a refactor land". Cycle 10
deliberately changes what the engine writes — prompt wording, selection, style repair —
so the golden is now a change **detector**, not a change **preventer**. What it detects
is an output change nobody meant to make. The rules:

* A phase that does NOT intend to change output leaves this file untouched. A diff there
  is still a real regression: find it, don't re-pin it.
* A phase that DOES change output re-pins the literals below **and records the exact
  before/after diff in that phase's entry in ``.autopilot/PLAN.md``**, so the change is
  reviewable as text rather than as a wall of new expected values.
* **A re-pin with no recorded diff is a failed checkpoint**, not a passing test. Pasting
  fresh output into the literals is exactly how a regression ships disguised as a
  refactor, and the recorded diff is the only thing standing in the way.

Four tests, each catching a different kind of drift:

* ``test_golden_bullet_pipeline`` — the transcript. Runs the stages by hand, so it also
  pins the *stage call order* (``_GOLDEN_STAGES``): a reordered or dropped LLM stage shows
  up as a sequence diff before the text diff does.
* ``test_run_tailor_matches_the_golden`` — drives the real ``run.tailor()`` with
  ``enforce_one_page`` stubbed at the render seam. This is the one SP2 has to satisfy:
  if the pass-pipeline refactor reorders anything, the bullets handed to
  ``enforce_one_page`` stop matching.
* ``test_grounding_gate_runs_at_every_bullet_pass`` — the gate is a no-op on grounded
  text, so the golden alone cannot tell four gate calls from three. This counts them and
  pins which one runs without a fallback.
* ``test_render_uses_the_real_template_preamble`` — the tests above stub
  ``assets.template_head()`` to a short sentinel so the golden ``.tex`` literal stays
  reviewable (the real preamble is 123 lines of candidate-independent LaTeX that no phase
  of this cycle touches). This test puts the real preamble back and proves ``render``
  still emits ``preamble + body`` verbatim, so nothing hides behind the sentinel.

Hermetic by construction, and it must stay that way:

* **No LLM call ever leaves the process.** The stub is installed as ``call`` on
  every ``resume_tailor.*`` module that has one (``compose``, ``llm`` and ``research``
  today, whatever SP4 adds tomorrow) — not just ``compose``, because a monkeypatch
  binds the name in the namespace where the function resolves its global, so patching
  ``compose`` alone would silently detach the moment SP4 moves ``select`` into
  ``selection.py``. Patching by sweep means this file survives that move untouched, which
  is the point. Any stage that reaches the stub with an unrecognised prompt raises
  instead of falling through to the network.
* **No user data reaches this file.** A synthetic master (``_MASTER``) replaces the real
  ``resume_tailor_files/master_experience.yaml``, which is gitignored personal data and
  absent on a fresh clone.
* **No pdflatex ever runs.** The pipeline stops at ``render.render()``; nothing compiles.
* **No config is left unpinned.** Every toggle the pipeline reads is pinned in ``pinned_engine``
  — the ``config.json`` map, the env-var overrides, the import-time constants, and the
  glyph-width capacities. A default flipped in ``config.py`` must fail its own test, not
  quietly rewrite this golden.
"""
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

from resume_tailor import (assets, compose, config, layout, measure, output,  # noqa: E402
                           render, verify)
from resume_tailor import apply_data  # noqa: E402
from resume_tailor import run as rt_run  # noqa: E402
from resume_tailor.compile import CompileResult  # noqa: E402

_CACHED = (
    assets.load_master, assets.tailor_config, assets.atoms_by_id, assets.blocks,
    assets.template_head, assets.skill_aliases, assets.skill_aliases_match_only,
)

# ── Fixed inputs ─────────────────────────────────────────────────────────────
# A synthetic candidate. Every atom's `what` is written so the bullets the stub
# returns are fully GROUNDED against it — verify.enforce_grounded traces each
# bullet's numbers and capitalized tokens back to its own group's atoms, so a
# careless word here would silently turn the golden into "the gate dropped it".
_MASTER = textwrap.dedent("""
    basics:
      name: Alex Rivera
      location: Austin, TX
      email: alex@example.com
      phone: "555-0142"
      linkedin: linkedin.com/in/alexrivera
    education:
      - school: State University
        location: Austin, TX
        degree: B.S. in Computer Science
        concentration: Data Science
        gpa: 3.8
        dates: "2021-08 / 2025-05"
        honors: [Dean's List]
    experience:
      - org: Globex Analytics
        title: Data Engineering Intern
        location: Austin, TX
        dates: "2024-06 / 2024-08"
        achievements:
          - id: gx_etl
            what: "Rebuilt the nightly ETL pipeline in Python against PostgreSQL and cut
                   batch runtime 42% while keeping the ingestion service green all summer"
            angles: [data]
          - id: gx_dbt
            what: "Modeled 12 dbt marts that replaced hand-written SQL extracts"
            angles: [data]
          - id: gx_tests
            what: "Added 63 regression tests to the ingestion service and wrote its runbook"
            angles: [quality]
      - org: Side Gig
        title: Retail Associate
        location: Austin, TX
        dates: "2023-01 / 2023-12"
        achievements:
          - id: sg_service
            what: "Served customers at a busy register"
            angles: [ops]
    projects:
      - name: Trailhead
        dates: "2024-09 / 2024-12"
        repo: github.com/alexrivera/trailhead
        achievements:
          - id: th_overview
            what: "Trailhead is a hiking route planner that ranks trails for a given
                   weather window"
            angles: [product]
          - id: th_model
            what: "Trained a gradient boosting model on 8,400 logged hikes to predict
                   trail difficulty"
            angles: [ml]
          - id: th_api
            what: "Exposed the planner through a FastAPI service backed by Redis caching"
            angles: [backend]
      - name: Ledgerly
        dates: "2024-02 / 2024-05"
        achievements:
          - id: ld_overview
            what: "Ledgerly is a personal budgeting tool that classifies bank transactions"
            angles: [product]
          - id: ld_parse
            what: "Parsed 30,000 CSV transactions with pandas and normalized merchant names"
            angles: [data]
    leadership:
      - org: Robotics Club
        dates: "2022-09 / 2024-05"
        achievements:
          - id: rc_lead
            what: "Led a team of 9 students to a regional finals placement"
            angles: [lead]
          - id: rc_workshop
            what: "Ran weekly Arduino workshops for 40 new members and grew club turnout"
            angles: [lead]
    skills:
      languages: [Python, SQL, R, Java]
      frameworks: [FastAPI, Flask, Django]
      developer_tools: [Git, Docker, PostgreSQL, Redis, dbt]
      libraries: [pandas, NumPy, scikit-learn]
      concepts_and_methodologies:
        - ETL
        - "A/B Testing"
        - Feature Engineering
        - "Exploratory Data Analysis (EDA)"
        - Data Modeling
    skill_aliases:
      "PostgreSQL": ["Postgres"]
      "A/B Testing": ["Experimentation"]
    skill_aliases_match_only:
      "Docker": ["Containerization"]
""")

# The JD is a fixed input too: it drives the Methods line's Tier-1 JD matching and the
# tech-line alias swap (it says "Postgres", so the Developer Tools line must print
# "Postgres", not "PostgreSQL"). "ETL" appears twice on purpose so its Tier-1 frequency
# rank is unambiguous.
_JD = (
    "Data Engineer, Analytics Platform. You will own the ETL that lands raw events in "
    "Postgres, model the marts our analysts query, and keep the nightly batch honest. "
    "Day to day you will write Python, review dbt models, and partner with product on "
    "Experimentation. Docker experience helps; ETL ownership is the core of the role."
)
_JOB = {
    "job_posting_id": "golden-0001",
    "company_name": "Initech",
    "job_title": "Data Engineer",
    "job_description": _JD,
    "url": "https://example.com/jobs/golden-0001",
}

# Every config toggle the bullet pipeline reads, pinned. See the module docstring.
_CONFIG_JSON = {
    "resume_layout_enabled": True,
    "resume_layout": {
        "Globex Analytics": {"line_targets": [2, 1]},
        "Side Gig": {"line_targets": [1]},
        "Robotics Club": {"line_targets": [1, 1]},
    },
    "project_layout": {"Trailhead": {"line_targets": [2, 1]}},
    "projects_max": 2,
    "projects_mode": "max",
    "fill_underfull": True,
    "lead_overview": True,
    "methods_line": True,
    "methods_line_label": "Methods",
    "tech_aliases": True,
    "verbatim_blocks": {
        "Side Gig": ["Built rapport with 120 regular customers and balanced the "
                     "register nightly."],
    },
}

# Pinned verb palette. dedupe_leading_verbs' deterministic arm picks the first UNUSED
# verb from the category holding the colliding one, so leaving this to the tracked
# active_words.md would couple the golden to an unrelated asset file.
_VERBS = {
    "Build": ["Built", "Designed", "Engineered", "Prototyped"],
    "Analyze": ["Analyzed", "Modeled", "Quantified"],
    "Lead": ["Led", "Coordinated", "Mentored"],
}

# Stand-in for the 123-line LaTeX preamble; see the module docstring and
# test_render_uses_the_real_template_preamble.
_STUB_HEAD = "%%GOLDEN TEMPLATE PREAMBLE%%\n\\begin{document}\n\n"


# ── The deterministic stubbed LLM ────────────────────────────────────────────
# One fixed payload per stage, dispatched on the stage's own system prompt. Each
# payload is shaped to exercise a branch the refactor could break:
#   select            — an over-long block/project so _enforce_fixed_counts really trims
#   lead_with_overview— a pick for Trailhead, SILENCE for Ledgerly (file-order fallback)
#   rephrase          — one over-length bullet (trim), one underfull (fill), one buzzword
#                       and one em dash (style gate), and two openers colliding with the
#                       verbatim block's "Built" (dedupe)
#   reverb            — succeeds for one collision, returns an ALREADY-USED verb for the
#                       other, so both dedupe arms (LLM re-roll + palette swap) run
#   fill_underfull    — folds Trailhead's spare th_api atom into the overview bullet
#   enforce_style     — repairs the buzzword bullet, declines the em-dash one so the
#                       mechanical backstop is what strips it
_SELECT = {
    "experience": [
        {"name": "Globex Analytics",
         "groups": [["gx_etl"], ["gx_dbt"], ["gx_tests"]]},   # 3 -> trimmed to 2
        {"name": "Side Gig", "groups": [["sg_service"]]},
    ],
    "projects": [
        {"name": "Trailhead", "groups": [["th_model"], ["th_overview"]]},
        {"name": "Ledgerly", "groups": [["ld_parse"], ["ld_overview"]]},
    ],
    "leadership": [{"name": "Robotics Club", "groups": [["rc_lead"], ["rc_workshop"]]}],
    "skill_focus": "data_analytics",
    "skills": {
        "Languages": "Python, SQL, R",
        "Frameworks": "FastAPI, Flask",
        "Developer Tools": "PostgreSQL, dbt, Docker, Git",
        "Libraries": "pandas, NumPy, scikit-learn",
    },
    "methods": ["ETL", "Data Modeling", "A/B Testing", "Feature Engineering"],
    "rationale": "Leads with the ETL and dbt evidence the posting centers on.",
}

# Trailhead's overview is bullet 2; Ledgerly is deliberately absent so the deterministic
# file-order fallback (earliest-authored atom leads) is exercised alongside the model pick.
_LEAD = {"projects": [{"project": "Trailhead", "lead": 2}]}

_BRIEFS = {"briefs": [
    {"block": "Globex Analytics", "brief": "Warehouse reliability, newest work first."},
    {"block": "Trailhead", "brief": "Say what the planner is, then how it ranks."},
    {"block": "Ledgerly", "brief": "Say what the tool is, then the data volume."},
    {"block": "Robotics Club", "brief": "Team outcome first, then the teaching."},
]}

_REPHRASE = {"bullets": [
    # Deliberately past its 2-line budget, so _trim_to_caps has real work to do.
    {"gkey": "gx_etl",
     "text": "Rebuilt the nightly ETL pipeline in Python against PostgreSQL and cut "
             "batch runtime 42%, keeping the ingestion service green across the whole "
             "summer while the warehouse kept taking on new raw event volume, and wrote "
             "the runbook that the on-call rotation followed all season."},
    # "Streamlined" is a banned buzzword verb -> repaired by the style gate.
    {"gkey": "gx_dbt",
     "text": "Streamlined 12 dbt marts that replaced hand-written SQL extracts."},
    # Opens with the verbatim block's reserved "Built" -> dedupe re-rolls via reverb.
    # Short of its 2-line budget -> the fill pass folds in the spare th_api atom.
    {"gkey": "th_overview",
     "text": "Built Trailhead, a hiking route planner that ranks trails for a given "
             "weather window."},
    # Also opens with "Built" -> reverb answers with an already-used verb -> palette swap.
    {"gkey": "th_model",
     "text": "Built a gradient boosting model on 8,400 logged hikes to predict trail "
             "difficulty."},
    {"gkey": "ld_overview",
     "text": "Shipped Ledgerly, a personal budgeting tool that classifies bank "
             "transactions."},
    {"gkey": "ld_parse",
     "text": "Parsed 30,000 CSV transactions with pandas and normalized merchant names."},
    {"gkey": "rc_lead",
     "text": "Led a team of 9 students to a regional finals placement."},
    # The em dash the mechanical backstop has to strip.
    {"gkey": "rc_workshop",
     "text": "Ran weekly Arduino workshops for 40 new members — growing club turnout "
             "through the year."},
]}

_FILL = {"bullets": [
    {"gkey": "th_overview",
     "text": "Designed Trailhead, a hiking route planner that ranks trails for a given "
             "weather window and serves them through a FastAPI service backed by Redis "
             "caching."},
]}

_STYLE = {"bullets": [
    {"gkey": "gx_dbt",
     "text": "Consolidated 12 dbt marts that replaced hand-written SQL extracts."},
    # Returned unrepaired on purpose: the em dash survives, so enforce_style must REJECT
    # this (violations not reduced) and let the unconditional strip handle it.
    {"gkey": "rc_workshop",
     "text": "Ran weekly Arduino workshops for 40 new members — growing club turnout "
             "through the year."},
]}


def _make_stub(stages):
    """A ``compose.call`` replacement that dispatches on the stage's system prompt and
    appends the stage name to `stages`. Unknown prompts raise: an unstubbed stage must
    fail loudly here, never fall through to a paid API."""
    def _call(system, user, tier, **kw):
        if "PURE SELECTION" in system:
            stages.append("select")
            return _SELECT
        if "PURE ORDERING" in system:
            stages.append("lead_with_overview")
            return _LEAD
        if "You frame resume blocks for cohesion" in system:
            stages.append("block_briefs")
            return _BRIEFS
        if "faithfully RE-PHRASING" in system:
            stages.append("rephrase")
            return _REPHRASE
        if "OPENS WITH A DIFFERENT action verb" in system:
            stages.append("reverb")
            if "gradient boosting model" in user:
                # An already-used opener: forces the deterministic palette swap.
                return {"text": "Designed a gradient boosting model on 8,400 logged "
                                "hikes to predict trail difficulty."}
            return {"text": "Designed Trailhead, a hiking route planner that ranks "
                            "trails for a given weather window."}
        if "You lengthen UNDERFULL" in system:
            stages.append("fill_underfull")
            return _FILL
        if "slipped into banned phrasing" in system:
            stages.append("enforce_style")
            return _STYLE
        if "EXACTLY FOUR fixed lines" in system:   # compress_skills' fallback call
            raise AssertionError(
                "compress_skills fell back to its own LLM call — select()'s skills "
                "should have been reused")
        raise AssertionError(f"unstubbed LLM stage: {system[:120]!r}")
    return _call


def _install_stub(monkeypatch, stub):
    """Bind `stub` as ``call`` on EVERY imported ``resume_tailor`` module that has one.

    A monkeypatch binds the name in the namespace where the function resolves its
    global, so ``setattr(compose, "call", ...)`` stops covering ``select`` the moment
    SP4 moves it into ``selection.py``. Sweeping every module keeps this file valid
    across that move — and keeps the money guard total: no module is left holding the
    real ``llm.call``.
    """
    patched = []
    for name, mod in sorted(sys.modules.items()):
        if name.startswith("resume_tailor.") and hasattr(mod, "call"):
            monkeypatch.setattr(mod, "call", stub)
            patched.append(name)
    assert "resume_tailor.compose" in patched, patched
    return patched


# ── Fixtures ─────────────────────────────────────────────────────────────────
@pytest.fixture()
def pinned_engine(tmp_path, monkeypatch):
    """Every input the bullet pipeline reads, pinned to a fixed value.

    Returns the list the stub appends stage names to, so a test can assert the call
    order as well as the text.
    """
    master = tmp_path / "master_experience.yaml"
    master.write_text(_MASTER, encoding="utf-8")
    monkeypatch.setattr(config, "MASTER_YAML", master)
    monkeypatch.setattr(config, "_config_json", lambda: dict(_CONFIG_JSON))

    # Env beats config.json for every one of these, so an exported override on the
    # developer's machine would otherwise decide the golden.
    for var in ("RESUME_TAILOR_PROJECTS_MAX", "RESUME_TAILOR_PROJECTS_MODE",
                "RESUME_TAILOR_FILL_UNDERFULL", "RESUME_TAILOR_LEAD_OVERVIEW",
                "RESUME_TAILOR_METHODS_LINE", "RESUME_TAILOR_METHODS_LABEL",
                "RESUME_TAILOR_TECH_ALIASES", "RESUME_TAILOR_SKILL_TARGETS",
                "RESUME_TAILOR_CANDIDATE"):
        monkeypatch.delenv(var, raising=False)

    # Import-time constants: env can no longer reach them, so pin the attributes.
    # (config.MAX_LINE_CHARS was pinned here until SP3 retired it — the length budget is
    # derived from measure.BODY_LINE_CAPACITY below, which is pinned instead.)
    monkeypatch.setattr(config, "DEFAULT_LINE_TARGETS", [2, 2, 2])
    monkeypatch.setattr(config, "PROJECTS_MAX", 3)
    monkeypatch.setattr(config, "PROJECT_BULLETS_MAX", 2)
    monkeypatch.setattr(config, "PROJECT_BULLET_LINES", 2)
    monkeypatch.setattr(measure, "BODY_LINE_CAPACITY", 53464)
    monkeypatch.setattr(measure, "SKILL_LINE_CAPACITY", 53464)
    # SP3 made the fill fractions env-overridable at import time. UNDERFULL_FILL decides which
    # bullets fill_underfull rewrites, so an exported override would otherwise pick the golden.
    monkeypatch.setattr(measure, "FULL_LINE_FILL", 0.90)
    monkeypatch.setattr(measure, "LAST_LINE_FILL", 0.75)
    monkeypatch.setattr(measure, "UNDERFULL_FILL", 0.50)
    monkeypatch.setattr(layout, "LEADERSHIP_ENTRY_LINES", 2)

    # Prompt-only assets that would otherwise read files absent from a fresh clone
    # (resume_sample.pdf) or drift independently of this cycle (active_words.md).
    monkeypatch.setattr(assets, "example_text", lambda: "Exemplar voice, fixed.")
    monkeypatch.setattr(assets, "active_verbs", lambda: {k: list(v) for k, v in _VERBS.items()})

    for fn in _CACHED:
        fn.cache_clear()
    stages: list = []
    _install_stub(monkeypatch, _make_stub(stages))
    yield stages
    for fn in _CACHED:
        fn.cache_clear()


@pytest.fixture()
def stub_template_head(monkeypatch):
    monkeypatch.setattr(assets, "template_head", lambda: _STUB_HEAD)


# ── The pipeline under test ──────────────────────────────────────────────────
def _run_bullet_pipeline():
    """A line-for-line transcript of ``run.tailor()``'s bullet middle, stopping at
    ``render.render()``. Kept deliberately flat and duplicated from ``run.py`` rather
    than factored: this is the RECORDING of the order, so it must not move when the
    production sequence is refactored — that is what makes the diff meaningful."""
    jd, job_title, company = _JD, _JOB["job_title"], _JOB["company_name"]

    sel = compose.select(jd, job_title, company)
    verbatim = compose.inject_verbatim(sel)
    if config.lead_overview_enabled():
        compose.lead_with_overview(jd, job_title, sel)
    briefs = compose.block_briefs(jd, job_title, sel)
    bullets = rt_run._resolve_bullets(jd, job_title, sel, lambda _m: None, briefs=briefs)
    verify.enforce_grounded(sel, bullets)

    reserved = frozenset(compose.leading_verb(t) for t in verbatim.values())
    grounded_snap = dict(bullets)
    compose.dedupe_leading_verbs(bullets, compose.group_map(sel), jd, reserved=reserved)
    verify.enforce_grounded(sel, bullets, fallback=grounded_snap)
    if verbatim:
        bullets.update(verbatim)

    rt_run._trim_to_caps(sel, bullets)

    if config.fill_underfull_enabled():
        grounded_snap = dict(bullets)
        compose.fill_underfull(jd, job_title, sel, bullets)
        rt_run._trim_to_caps(sel, bullets)
        verify.enforce_grounded(sel, bullets, fallback=grounded_snap)

    grounded_snap = dict(bullets)
    compose.enforce_style(jd, job_title, sel, bullets)
    rt_run._trim_to_caps(sel, bullets)     # SP2: the style repair may lengthen a bullet
    verify.enforce_grounded(sel, bullets, fallback=grounded_snap)

    skill_lines = compose.compress_skills(jd, job_title, sel)
    if config.methods_line_enabled():
        methods = compose.methods_line(jd, sel)
        if methods:
            skill_lines.append(methods)

    return sel, bullets, skill_lines, render.render(sel, bullets, skill_lines)


# ── The golden ───────────────────────────────────────────────────────────────
# GENERATED, NOT HAND-WRITTEN: produced by running the pipeline above once and pasting
# what came out. Read the module docstring before touching any of it.
_GOLDEN_STAGES = [
    "select",
    "lead_with_overview",
    "block_briefs",
    "rephrase",
    "reverb",
    "reverb",
    "fill_underfull",
    "enforce_style",
]

_GOLDEN_BULLETS = {
    # SP2 re-pin (clause-cut floor 0.6 -> 0.85). Was "...new raw event volume": the only
    # comma in the over-budget prefix sat at char 204 of a 254-char 2-line budget (80%),
    # which cleared the old 60% floor, so the clause cut fired and threw away 50 chars
    # that FIT. It now falls through to the word cut, which keeps 232 of the 254 and lands
    # on "...the runbook" (_strip_dangling sheds the trailing "that ..." fragment).
    "gx_etl":
        "Rebuilt the nightly ETL pipeline in Python against PostgreSQL and cut batch runtime 42%, "
        "keeping the ingestion service green across the whole summer while the warehouse kept taking on "
        "new raw event volume, and wrote the runbook",
    "gx_dbt":
        "Consolidated 12 dbt marts that replaced hand-written SQL extracts.",
    "th_model":
        "Engineered a gradient boosting model on 8,400 logged hikes to predict trail difficulty.",
    "ld_overview":
        "Shipped Ledgerly, a personal budgeting tool that classifies bank transactions.",
    "ld_parse":
        "Parsed 30,000 CSV transactions with pandas and normalized merchant names.",
    "rc_lead":
        "Led a team of 9 students to a regional finals placement.",
    "rc_workshop":
        "Ran weekly Arduino workshops for 40 new members, growing club turnout through the year.",
    "__verbatim__/Side Gig/0":
        "Built rapport with 120 regular customers and balanced the register nightly.",
    "th_overview+th_api":
        "Designed Trailhead, a hiking route planner that ranks trails for a given weather window and "
        "serves them through a FastAPI service backed by Redis caching.",
}

_GOLDEN_SKILL_LINES = [
    {"label": "Languages", "items": "Python, SQL, R, Java"},
    {"label": "Frameworks", "items": "FastAPI, Flask, Django"},
    {"label": "Developer Tools", "items": "Postgres, dbt, Docker, Git, Redis"},
    {"label": "Libraries", "items": "pandas, NumPy, scikit-learn"},
    {"label": "Methods", "items": "ETL, Experimentation, Data Modeling, Feature Engineering"},
]

_GOLDEN_TEX = r"""%%GOLDEN TEMPLATE PREAMBLE%%
\begin{document}

\begin{center}
\textbf{\Huge \scshape Alex Rivera} \\ \vspace{1pt}
\small{Austin, TX} $|$ \small{555-0142} $|$ \small{alex@example.com} $|$ \small{linkedin.com/in/alexrivera}
\end{center}
\vspace{-10pt}

%-----------EDUCATION-----------
\section{Education}
\resumeSubHeadingListStart
\resumeSubheading
{State University $|$ 3.8 GPA}{August 2021 -- May 2025}
{B.S. in Computer Science with a Concentration in Data Science}{Austin, TX}\vspace{2pt}
\item \small{\textbf{Awards \& Honors:} Dean's List}
\resumeSubHeadingListEnd

\vspace{-10pt}


%-----------EXPERIENCE-----------
\section{Work Experience}
\resumeSubHeadingListStart

\resumeSubheadingOneLine
{Data Engineering Intern}{Globex Analytics}{Austin, TX}{June 2024 -- August 2024}
\resumeItemListStart
\resumeItem{Rebuilt the nightly ETL pipeline in Python against PostgreSQL and cut batch runtime 42\%, keeping the ingestion service green across the whole summer while the warehouse kept taking on new raw event volume, and wrote the runbook.}
\resumeItem{Consolidated 12 dbt marts that replaced hand-written SQL extracts.}
\resumeItemListEnd

\resumeSubheadingOneLine
{Retail Associate}{Side Gig}{Austin, TX}{January 2023 -- December 2023}
\resumeItemListStart
\resumeItem{Built rapport with 120 regular customers and balanced the register nightly.}
\resumeItemListEnd
\resumeSubHeadingListEnd

%-----------PROJECTS-----------
\section{Projects}
\resumeSubHeadingListStart

\resumeProjectHeadingInline
{Trailhead}{ $|$ \href{https://github.com/alexrivera/trailhead}{\textit{Link}}}
\resumeItemListStart
\resumeItem{Designed Trailhead, a hiking route planner that ranks trails for a given weather window and serves them through a FastAPI service backed by Redis caching.}
\resumeItem{Engineered a gradient boosting model on 8,400 logged hikes to predict trail difficulty.}
\resumeItemListEnd

\resumeProjectHeadingInline
{Ledgerly}{}
\resumeItemListStart
\resumeItem{Shipped Ledgerly, a personal budgeting tool that classifies bank transactions.}
\resumeItem{Parsed 30,000 CSV transactions with pandas and normalized merchant names.}
\resumeItemListEnd
\resumeSubHeadingListEnd

\vspace{-10pt}

%-----------Leadership Experience-----------
\section{Leadership Experience}
\resumeSubHeadingListStart
\resumeProjectHeading
{\textbf{Robotics Club}}{September 2022 -- May 2024}
\resumeItemListStart
\resumeItem{Led a team of 9 students to a regional finals placement.}
\resumeItem{Ran weekly Arduino workshops for 40 new members, growing club turnout through the year.}
\resumeItemListEnd
\resumeSubHeadingListEnd

%-----------Technical SKILLS-----------
\section{Technical Skills}
\begin{itemize}[leftmargin=0.15in, label={}]
\item \small{
\textbf{Languages}{: } Python, SQL, R, Java \\
\textbf{Frameworks}{: } FastAPI, Flask, Django \\
\textbf{Developer Tools}{: } Postgres, dbt, Docker, Git, Redis \\
\textbf{Libraries}{: } pandas, NumPy, scikit-learn \\
\textbf{Methods}{: } ETL, Experimentation, Data Modeling, Feature Engineering \\
}
\end{itemize}

%-------------------------------------------
\end{document}
"""


def test_golden_bullet_pipeline(pinned_engine, stub_template_head):
    """The whole bullet pipeline, stage by stage, against the frozen recording."""
    _sel, bullets, skill_lines, tex = _run_bullet_pipeline()
    assert pinned_engine == _GOLDEN_STAGES
    assert bullets == _GOLDEN_BULLETS
    assert skill_lines == _GOLDEN_SKILL_LINES
    assert tex == _GOLDEN_TEX


def test_run_tailor_matches_the_golden(pinned_engine, stub_template_head,
                                       tmp_path, monkeypatch):
    """The same golden, but produced by the real ``run.tailor()``.

    Everything past the render seam is stubbed out — ``enforce_one_page`` captures what
    it was handed and hands back a fake PDF, ``pdflatex_available`` lies, and the
    advisory apply-sheet writer is silenced (it reaches stores this test has no business
    touching). What is NOT stubbed is the stage sequencing, which is exactly what SP2
    rewrites.
    """
    captured: dict = {}

    def _fake_enforce(sel, bullets, skill_lines, tex_path, work_dir, jd="",
                      on_status=None, keep_projects=None):
        tex = render.render(sel, bullets, skill_lines)
        Path(tex_path).write_text(tex, encoding="utf-8")
        pdf = Path(work_dir) / "golden.pdf"
        pdf.write_bytes(b"%PDF-1.4 golden stub\n")
        captured.update(bullets=dict(bullets), skill_lines=list(skill_lines), tex=tex)
        return CompileResult(True, pdf, ""), dict(bullets), tex

    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setattr(rt_run, "pdflatex_available", lambda: True)
    monkeypatch.setattr(rt_run, "enforce_one_page", _fake_enforce)
    monkeypatch.setattr(output, "resolve_dir", lambda *a, **k: out)
    monkeypatch.setattr(apply_data, "write", lambda *a, **k: None)

    assert rt_run.tailor(_JOB, ats_report=False) == out
    assert pinned_engine == _GOLDEN_STAGES
    assert captured["bullets"] == _GOLDEN_BULLETS
    assert captured["skill_lines"] == _GOLDEN_SKILL_LINES
    assert captured["tex"] == _GOLDEN_TEX
    assert (out / "resume.tex").read_text(encoding="utf-8") == _GOLDEN_TEX


def test_render_uses_the_real_template_preamble(pinned_engine):
    """With the sentinel removed, the rendered .tex is the REAL preamble + the golden
    body — so nothing about the output is hiding behind the stubbed head."""
    _sel, _bullets, _skills, tex = _run_bullet_pipeline()
    assert tex == assets.template_head() + _GOLDEN_TEX[len(_STUB_HEAD):]


def test_grounding_gate_runs_at_every_bullet_pass(pinned_engine, stub_template_head,
                                                  tmp_path, monkeypatch):
    """The grounding gate is called FOUR times, and only the first one runs without a
    fallback.

    The golden above can't see this on its own: with everything grounded the gate is a
    no-op, so a refactor that quietly lost a call site would still produce identical
    text. The snapshot -> mutate -> re-verify discipline is precisely what SP2 makes
    structural, so pin the shape of it — one fallback-less prologue gate after rephrase,
    then one fallback-bearing gate after each bullet-mutating pass (verb dedupe,
    underfull fill, style gate).
    """
    calls: list = []
    real_gate = verify.enforce_grounded

    def _recording_gate(sel, bullets, *, fallback=None, log=None):
        calls.append(fallback is None)
        return real_gate(sel, bullets, fallback=fallback, log=log)

    def _fake_enforce(sel, bullets, skill_lines, tex_path, work_dir, jd="",
                      on_status=None, keep_projects=None):
        tex = render.render(sel, bullets, skill_lines)
        Path(tex_path).write_text(tex, encoding="utf-8")
        pdf = Path(work_dir) / "golden.pdf"
        pdf.write_bytes(b"%PDF-1.4 golden stub\n")
        return CompileResult(True, pdf, ""), dict(bullets), tex

    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setattr(verify, "enforce_grounded", _recording_gate)
    monkeypatch.setattr(rt_run, "pdflatex_available", lambda: True)
    monkeypatch.setattr(rt_run, "enforce_one_page", _fake_enforce)
    monkeypatch.setattr(output, "resolve_dir", lambda *a, **k: out)
    monkeypatch.setattr(apply_data, "write", lambda *a, **k: None)

    rt_run.tailor(_JOB, ats_report=False)
    assert calls == [True, False, False, False]
