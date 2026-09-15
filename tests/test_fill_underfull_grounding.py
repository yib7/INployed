"""`compose.fill_underfull` must gate its OWN commit, not trust the driver's gate.

`fill_underfull` grows an underfull bullet by folding in one spare atom, then commits
the fill as GROUP AUGMENTATION: it appends the spare id to that group in `sel` and
re-keys `bullets[old_gk]` onto `bullets[new_gk]`. `run._run_bullet_passes` snapshots
`ctx.bullets` BEFORE the pass runs and hands that snapshot to the grounding gate as its
revert target -- but a committed fill is keyed under `new_gk`, which the snapshot never
held, so a bullet the gate has to flag is DROPPED outright instead of reverted to its
grounded original. A real run lost its lead bullet this way (a 2026-09-14 tailor
run) even though the pre-fill text was perfectly grounded.

Tests 1-3 hit `compose.fill_underfull` directly, at the unit the fix lives in. Tests 4-5
go through the real `Pass` + `_run_bullet_passes` wiring, because the regression itself
is a mismatch between two files (compose.py's re-key, run.py's snapshot) a
compose-only test cannot see.

**Nothing here calls a model.** `compose.call` is monkeypatched in every test.
"""
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

from resume_tailor import assets, compose, config, measure  # noqa: E402
from resume_tailor import run as rt_run  # noqa: E402

_CACHED = (
    assets.load_master, assets.tailor_config, assets.atoms_by_id,
    assets.blocks, assets.template_head, assets.skill_aliases,
    assets.skill_aliases_match_only,
)

# One project, "Sandbox", with exactly the two atoms this file needs: `s_over` is the
# bullet under test and `s_audit` is its only spare -- the one atom fill_underfull may
# legitimately fold in. `s_audit` carries a dotted version string on purpose: the
# driver-level tests (4, 5) are also the guard that Task 1's tokenizer fix (a dotted
# version is one figure) and this task's gate do not re-break each other.
_MASTER = textwrap.dedent("""
    basics:
      name: Jane Q. Public
      location: City, ST
      email: jane@example.com
      phone: "555-0100"
    education:
      - school: State University
        location: City, ST
        degree: B.S. in Computer Science
        dates: "2021-08 / 2025-05"
    experience:
      - org: Example Co
        title: Engineer
        location: City, ST
        dates: "2024-06 / 2024-08"
        achievements:
          - {id: e_a, what: "did a thing"}
    projects:
      - name: Sandbox
        dates: "2024-01 / 2024-05"
        achievements:
          - {id: s_over, what: "Built a data-analysis web platform that turns plain requests into runnable Python workflows"}
          - {id: s_audit, what: "A re-audit five weeks after the v1.4.0 release closed 2 bypasses of the static pre-check"}
    leadership:
      - org: Example Club
        dates: "2023-09 / 2024-05"
        achievements:
          - {id: l_a, what: "led members"}
""")

# The bullet as it ships before any fill: a direct re-phrase of `s_over`'s own words,
# so every token traces trivially.
ORIGINAL = ("Built a data-analysis web platform that turns plain requests into runnable "
           "Python workflows.")

# A fill that introduces "Deloitte" -- a token neither atom supports, even counting the
# spare `s_audit` the augmented group adds. "v1.4.0" is IN this text too, but it is
# grounded (it is `s_audit`'s own figure); "Deloitte" is the only unseen token.
UNGROUNDED_FILL = ("Built a data-analysis web platform that turns plain requests into "
                   "runnable Python workflows, audited by Deloitte after the v1.4.0 "
                   "release.")

# A fill that folds in ONLY `s_audit`'s own material -- including its dotted version
# string. Must commit both before and after this task; it is the guard that the new
# gate does not over-reject a legitimate fill.
GROUNDED_FILL = ("Built a data-analysis web platform that turns plain requests into "
                 "runnable Python workflows, re-audited five weeks after the v1.4.0 "
                 "release.")


@pytest.fixture()
def synthetic_master(tmp_path, monkeypatch):
    p = tmp_path / "master.yaml"
    p.write_text(_MASTER, encoding="utf-8")
    monkeypatch.setattr(config, "MASTER_YAML", p)
    monkeypatch.delenv("RESUME_TAILOR_CANDIDATE", raising=False)
    for fn in _CACHED:
        fn.cache_clear()
    yield p
    for fn in _CACHED:
        fn.cache_clear()


@pytest.fixture()
def engine(synthetic_master, monkeypatch):
    """Every knob the fill and the driver read, pinned -- the same combination the
    reground/sweep pass-driver tests use, so `s_over` counts as underfull at a 2-line
    target and nothing gets trimmed back out from under the assertions."""
    monkeypatch.setattr(config, "_config_json", lambda: {})
    monkeypatch.setattr(measure, "BODY_LINE_CAPACITY", 53464)
    monkeypatch.setattr(config, "DEFAULT_LINE_TARGETS", [2, 2])
    monkeypatch.setattr(config, "PROJECT_BULLET_LINES", 2)
    monkeypatch.setattr(config, "verbatim_blocks", lambda: {})
    monkeypatch.delenv("RESUME_TAILOR_FILL_UNDERFULL", raising=False)


def _fake_bullets(*pairs):
    """Build a compose.call stub returning {"bullets":[{gkey,text}, ...]} verbatim."""
    def _call(system, user, tier, **kw):
        return {"bullets": [{"gkey": gk, "text": txt} for gk, txt in pairs]}
    return _call


def _sel():
    return {"experience": [], "leadership": [],
            "projects": [{"name": "Sandbox", "groups": [["s_over"]]}]}


def test_an_ungrounded_fill_is_not_committed(engine, monkeypatch):
    """Pins the defect at its source: a fill that introduces a token neither atom
    supports must never be committed. RED today: the bullet is re-keyed to
    's_over+s_audit' carrying the ungrounded text regardless."""
    sel = _sel()
    bullets = {"s_over": ORIGINAL}
    monkeypatch.setattr(compose, "call", _fake_bullets(("s_over", UNGROUNDED_FILL)))
    compose.fill_underfull("a long job description", "Engineer", sel, bullets)
    assert bullets == {"s_over": ORIGINAL}
    assert sel["projects"][0]["groups"] == [["s_over"]]


def test_a_rejected_fill_is_reported_with_its_tokens_and_text(engine, monkeypatch):
    """Pins the on_reject contract: the caller needs the gkey, the unseen tokens, and
    the rejected text verbatim to write a useful report line. RED today: TypeError,
    fill_underfull takes no 'on_reject' keyword."""
    sel = _sel()
    bullets = {"s_over": ORIGINAL}
    monkeypatch.setattr(compose, "call", _fake_bullets(("s_over", UNGROUNDED_FILL)))
    calls = []
    compose.fill_underfull("a long job description", "Engineer", sel, bullets,
                           on_reject=lambda *a: calls.append(a))
    assert calls == [("s_over", ["Deloitte"], UNGROUNDED_FILL)]


def test_a_grounded_fill_still_commits_and_rekeys(engine, monkeypatch):
    """Guards against over-rejection: a fill that folds in only the spare atom's own
    material -- including its dotted version string -- must still commit and re-key.
    This is the SP1+SP2 trigger case (with this gate in place it fails on the
    pre-Task-1 tokenizer, so it also guards that fix); green before and after this
    task, on purpose."""
    sel = _sel()
    bullets = {"s_over": ORIGINAL}
    monkeypatch.setattr(compose, "call", _fake_bullets(("s_over", GROUNDED_FILL)))
    calls = []
    out = compose.fill_underfull("a long job description", "Engineer", sel, bullets,
                                 on_reject=lambda *a: calls.append(a))
    assert "s_over+s_audit" in out
    assert "s_over" not in out
    assert out["s_over+s_audit"] == GROUNDED_FILL
    assert sel["projects"][0]["groups"] == [["s_over", "s_audit"]]
    assert calls == []


def test_the_pass_driver_keeps_the_bullet_when_a_fill_is_ungrounded(engine, monkeypatch):
    """The regression itself, through the REAL wiring. Before this task the pass had
    no gate of its own, so the fill committed the ungrounded text; the driver's OWN
    grounding gate then looked for the pre-fill text under the fallback snapshot's OLD
    gkey, found nothing (the snapshot never held the NEW, re-keyed gkey), and dropped
    the bullet outright. RED today: ctx.bullets == {} and a warning
    "grounding: [underfull fill] dropped bullet 's_over+s_audit' (ungrounded: Deloitte)"."""
    sel = _sel()
    bullets = {"s_over": ORIGINAL}
    monkeypatch.setattr(compose, "call", _fake_bullets(("s_over", UNGROUNDED_FILL)))
    ctx = rt_run.PassCtx(jd="a long job description", job_title="Engineer", sel=sel,
                         bullets=bullets, verbatim={}, reserved=frozenset(),
                         log=lambda _m: None, report=rt_run.RunLog())
    fill_pass = next(p for p in rt_run._BULLET_PASSES if p.name == "underfull fill")
    rt_run._run_bullet_passes(ctx, passes=(fill_pass,))
    assert ctx.bullets == {"s_over": ORIGINAL}
    assert sel["projects"][0]["groups"] == [["s_over"]]
    assert ctx.report.warnings == []
    assert any(
        "[underfull fill] refused a fill for 's_over'" in line
        and "Deloitte" in line and UNGROUNDED_FILL in line
        for line in ctx.report.note_lines
    )


def test_the_pass_driver_commits_a_fill_that_copies_the_atoms_own_version_string(
        engine, monkeypatch):
    """Driver-level parity check for test 3: the atoms' own v1.4.0 must still reach the
    page through the real Pass + _run_bullet_passes wiring, with no spurious refusal."""
    sel = _sel()
    bullets = {"s_over": ORIGINAL}
    monkeypatch.setattr(compose, "call", _fake_bullets(("s_over", GROUNDED_FILL)))
    ctx = rt_run.PassCtx(jd="a long job description", job_title="Engineer", sel=sel,
                         bullets=bullets, verbatim={}, reserved=frozenset(),
                         log=lambda _m: None, report=rt_run.RunLog())
    fill_pass = next(p for p in rt_run._BULLET_PASSES if p.name == "underfull fill")
    rt_run._run_bullet_passes(ctx, passes=(fill_pass,))
    assert "s_over+s_audit" in ctx.bullets
    assert ctx.report.warnings == []
    assert not any("refused" in line for line in ctx.report.note_lines)
