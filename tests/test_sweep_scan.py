"""The résumé scan in front of the AI-writing sweep (`sweep.sweep_items`).

With Jev off, or when Jev's read fails, the sweep first asks the model ONE
detect-only question over every bullet it may rewrite, grouped by entry: which
bullets carry an AI-writing tell, and which tells. An entry then makes its repair
call only when the scan flagged one of its bullets or a detector forces it (a
repaired-tier `itemcheck` finding or an `aiwriting` phrasing hit), the same gate
Jev's read drives. The scan's tells ride in the payload as `judge_flags` with the
flag rule in the system prompt. A scan that fails leaves the detectors to decide,
exactly as with the toggle off: only a detector finding sends an entry.

Nothing here reaches a model or Jev: `sweep.call` is a recorder that answers the
scan and the repair call from fixtures, and every judge is a fake.
"""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

from resume_tailor import aiwriting, compose, config, jev_assist, sweep  # noqa: E402
from resume_tailor import run as rt_run  # noqa: E402

import test_item_sweep as item_sweep  # noqa: E402 - sibling test module, no pkg import
import test_sweep_gate as sweep_gate  # noqa: E402

# The sweep module's fixture, re-exported by assignment (see
# test_sweep_layout_invariant.py for why not `from ... import`).
engine = item_sweep.engine

ITEM = item_sweep.ITEM
CLEAN = item_sweep.CLEAN
Recorder = item_sweep.Recorder
_answer = item_sweep._answer
_install = item_sweep._install
_sel = item_sweep._sel
_two_items = sweep_gate._two_items
detector_gate = item_sweep.detector_gate
Reads = sweep_gate.Reads
Failing = sweep_gate.Failing
_V1 = sweep_gate._V1

SCAN_ENV = "RESUME_TAILOR_SWEEP_SCAN"


@pytest.fixture(autouse=True)
def _scan_on(engine, monkeypatch):
    """`engine` pins the scan off for the sweep's own tests; this file turns it back
    on by clearing the env after `engine` has run, so the default (on) decides."""
    monkeypatch.delenv(SCAN_ENV, raising=False)
    jev_assist.reset_usage()
    yield
    jev_assist.reset_usage()


class ScanRecorder(Recorder):
    """`Recorder`, plus the scan. `scan` answers the scan call: a value is returned, an
    exception is raised, and None means the scan was not expected and raises."""

    def __init__(self, scan=None, first=None, second=None):
        super().__init__(first=first, second=second)
        self.scan = scan
        self.options = []        # [(kind, tier, kw), ...]

    def __call__(self, system, user, tier, **kw):
        if system == getattr(sweep, "_SCAN_SYSTEM", None):
            self.calls.append(("scan", system, user))
            self.options.append(("scan", tier, kw))
            if isinstance(self.scan, BaseException):
                raise self.scan
            if self.scan is None:
                raise AssertionError("the sweep issued an unexpected scan")
            return self.scan
        self.options.append(("other", tier, kw))
        return super().__call__(system, user, tier, **kw)


def _scan_body(user):
    """The JSON body of the scan's user prompt: the first `{` and its matching `}`."""
    start = user.index("{")
    depth = 0
    for i, ch in enumerate(user[start:], start):
        depth += (ch == "{") - (ch == "}")
        if depth == 0:
            return json.loads(user[start:i + 1])
    raise AssertionError("no JSON body in the scan prompt")


def _flags(**tells):
    return {"flags": [{"gkey": gk, "tells": list(t)} for gk, t in tells.items()]}


_CLEAN_SCAN = {"flags": []}


def _sweep(monkeypatch, bullets, sel=None, scan=None, first=None, **kw):
    rec = _install(monkeypatch, ScanRecorder(scan=scan, first=first))
    result = sweep.sweep_items("", "Data Engineer", sel or _sel(), bullets, **kw)
    return rec, result


# ── the toggle ───────────────────────────────────────────────────────────────
def test_the_scan_is_on_by_default(engine):
    assert config.sweep_scan_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off"])
def test_the_env_turns_the_scan_off(engine, monkeypatch, value):
    monkeypatch.setenv(SCAN_ENV, value)
    assert config.sweep_scan_enabled() is False


def test_config_json_turns_the_scan_off(engine, monkeypatch):
    monkeypatch.setattr(config, "_config_json", lambda: {"resume_sweep_scan": False})
    assert config.sweep_scan_enabled() is False


def test_the_env_beats_config_json(engine, monkeypatch):
    monkeypatch.setattr(config, "_config_json", lambda: {"resume_sweep_scan": False})
    monkeypatch.setenv(SCAN_ENV, "1")
    assert config.sweep_scan_enabled() is True


def test_a_blank_env_falls_through_to_config_json(engine, monkeypatch):
    monkeypatch.setenv(SCAN_ENV, "  ")
    monkeypatch.setattr(config, "_config_json", lambda: {"resume_sweep_scan": False})
    assert config.sweep_scan_enabled() is False


def test_a_stray_string_in_config_json_reads_as_on(engine, monkeypatch):
    """The spelling every ON-by-default toggle in config.py uses."""
    monkeypatch.setattr(config, "_config_json", lambda: {"resume_sweep_scan": "false"})
    assert config.sweep_scan_enabled() is True


def test_the_settings_schema_exposes_the_toggle_defaulting_on():
    import settings  # noqa: PLC0415 - imported here so the sys.path insert applies

    field = next(f for f in settings.SETTINGS_SCHEMA if f.key == "resume_sweep_scan")
    assert (field.type, field.default, field.section, field.target) == (
        "bool", True, "Resume", "config")


def test_the_env_example_documents_the_toggle():
    text = (REPO / ".env.example").read_text(encoding="utf-8")
    assert any(ln.startswith(f"# {SCAN_ENV}=1") for ln in text.splitlines())


# ── the gate ────────────────────────────────────────────────────────────────
def test_a_clean_scan_makes_one_call_and_no_repair_call(engine, monkeypatch):
    """The recorder raises on any repair call (`first` is None)."""
    bullets = dict(CLEAN)
    rec, result = _sweep(monkeypatch, bullets, scan=_CLEAN_SCAN)
    assert rec.kinds == ["scan"]
    assert bullets == CLEAN
    assert result.scan == sweep.SCAN_RAN
    assert result.skipped == (ITEM,)
    assert result.judge_flags == ()
    assert (result.calls, result.items) == (1, 0)


def test_one_scan_covers_every_entry(engine, monkeypatch):
    sel, bullets = _two_items()
    rec, result = _sweep(monkeypatch, bullets, sel=sel, scan=_CLEAN_SCAN)
    assert rec.kinds == ["scan"]
    assert result.skipped == (ITEM, "Trailhead")
    assert (result.calls, result.items) == (1, 0)


def test_the_scan_reads_the_bullets_the_sweep_would_send_grouped_by_entry(engine,
                                                                          monkeypatch):
    sel, bullets = _two_items()
    rec, _result = _sweep(monkeypatch, bullets, sel=sel, scan=_CLEAN_SCAN)
    _kind, _system, user = rec.calls[0]
    assert _scan_body(user) == {"entries": [
        {"entry": ITEM, "bullets": [{"gkey": gk, "text": CLEAN[gk]}
                                    for gk in ("a1", "a2", "a3")]},
        {"entry": "Trailhead", "bullets": [{"gkey": "v1", "text": _V1}]}]}


def test_the_scan_is_a_low_temperature_flash_json_call(engine, monkeypatch):
    rec, _result = _sweep(monkeypatch, dict(CLEAN), scan=_CLEAN_SCAN)
    assert rec.options == [("scan", config.TIER_FLASH,
                            {"json_out": True, "temperature": 0.2})]


def test_the_scan_never_sees_a_verbatim_bullet(engine, monkeypatch):
    vgk = "__verbatim__/Example Corp/0"
    sel = _sel(groups=(["a1"], [vgk]))
    bullets = {"a1": CLEAN["a1"], vgk: "My own bullet."}
    rec, _result = _sweep(monkeypatch, bullets, sel=sel, scan=_CLEAN_SCAN)
    assert "My own bullet." not in rec.calls[0][2]


def test_no_bullet_means_no_scan(engine, monkeypatch):
    rec, result = _sweep(monkeypatch, {}, scan=None)
    assert rec.calls == []
    assert result.scan == ""
    assert result.calls == 0


def test_a_flagged_bullet_sends_only_its_entry_with_the_flags_and_the_rule(engine,
                                                                          monkeypatch):
    sel, bullets = _two_items()
    rec, result = _sweep(monkeypatch, bullets, sel=sel,
                         scan=_flags(a2=["hype words"]), first=_answer())
    assert rec.kinds == ["scan", "sweep"]
    _kind, system, user = rec.calls[1]
    assert system == sweep._SWEEP_SYSTEM + sweep._SWEEP_FLAG_RULE
    assert sweep._SWEEP_FLAG_CLOSING in user
    sent = {b["gkey"]: b for b in rec.payload(1)["bullets"]}
    assert rec.payload(1)["item"] == ITEM
    assert sent["a2"]["judge_flags"] == ["hype words"]
    assert sent["a1"]["judge_flags"] == [] and sent["a3"]["judge_flags"] == []
    assert result.judge_flags == (sweep.JudgeFlag("a2", ITEM, ("hype words",)),)
    assert result.skipped == ("Trailhead",)
    assert (result.calls, result.items) == (2, 1)


def test_a_flag_is_held_to_the_same_acceptance_check(engine, monkeypatch):
    dropped = ("Raised test coverage on the billing service with pytest suites for its "
               "three least-covered modules.")
    bullets = dict(CLEAN)
    _rec, result = _sweep(monkeypatch, bullets, scan=_flags(a2=["filler"]),
                          first=_answer(a2=dropped))
    assert bullets == CLEAN
    assert [(r.gkey, r.reason) for r in result.rejected] == [("a2", sweep.REASON_FACTS)]


@pytest.mark.parametrize("bullets,why", [
    pytest.param(dict(CLEAN, a1=item_sweep._BARE), "bare_noun_bullet", id="p1-finding"),
    pytest.param(dict(CLEAN, a1=item_sweep._FORMULAIC), "formulaic opening", id="phrasing"),
])
def test_a_detector_hit_forces_the_call_despite_a_clean_scan(engine, monkeypatch,
                                                              bullets, why):
    """No tell to name, so the repair call is the one the sweep sends with the scan
    off, byte for byte."""
    monkeypatch.setenv(SCAN_ENV, "0")
    off, _r = _sweep(monkeypatch, dict(bullets), first=_answer())
    monkeypatch.delenv(SCAN_ENV)
    rec, result = _sweep(monkeypatch, dict(bullets), scan=_CLEAN_SCAN, first=_answer())
    assert rec.kinds == ["scan", "sweep"]
    sent = {b["gkey"]: b for b in rec.payload(1)["bullets"]}
    assert why in sent["a1"]["findings"] + sent["a1"]["phrasing"]
    assert rec.calls[1:] == off.calls
    assert result.skipped == ()


# ── parsing the answer ──────────────────────────────────────────────────────
def test_tells_are_trimmed(engine, monkeypatch):
    long_tell = "x" * 500
    scan = {"flags": [{"gkey": "a3", "tells": ["  stacked adjectives  ", "", long_tell]}]}
    rec, result = _sweep(monkeypatch, dict(CLEAN), scan=scan, first=_answer())
    assert rec.kinds == ["scan", "sweep"]
    (flag,) = result.judge_flags
    assert flag.gkey == "a3"
    assert flag.names[0] == "stacked adjectives"
    assert len(flag.names) == 2
    assert len(flag.names[1]) <= sweep._SCAN_TELL_MAX


@pytest.mark.parametrize("tells", [[], [""], ["   "]], ids=["empty", "blank", "spaces"])
def test_a_flag_with_no_usable_tell_still_buys_the_repair(engine, monkeypatch, tells):
    """The model listed the bullet, so it meant to flag it: the repair runs, under a
    generic tell name."""
    rec, result = _sweep(monkeypatch, dict(CLEAN),
                         scan={"flags": [{"gkey": "a1", "tells": tells}]}, first=_answer())
    assert rec.kinds == ["scan", "sweep"]
    assert result.scan == sweep.SCAN_RAN
    (flag,) = result.judge_flags
    assert (flag.gkey, flag.names) == ("a1", (sweep._SCAN_UNNAMED,))


@pytest.mark.parametrize("scan", [
    {"flags": [{"gkey": "a2", "tells": ["filler"]}, {"gkey": "V1", "tells": ["hype words"]}]},
    {"flags": [{"gkey": "a2", "tells": ["filler"]}, {"gkey": "zz", "tells": ["hype words"]}]},
    {"flags": [{"gkey": "v1", "tells": [""]}, {"gkey": "a2", "tells": None}]},
    {"flags": [{"gkey": "v1", "tells": ["hype words"]}, {"gkey": "a2", "tells": {}}]},
    {"flags": [{"gkey": "v1", "tells": ["hype words"]}, {"gkey": "a2"}]},
    {"flags": [{"gkey": "v1", "tells": ["hype words"]}, "a2"]},
], ids=["wrong-case-gkey", "unknown-gkey", "tells-null", "tells-object", "tells-missing",
        "bare-gkey"])
def test_one_bad_flag_fails_the_whole_scan(engine, detector_gate, monkeypatch, scan):
    """A good flag in one entry does not carry a bad flag in another: dropping the bad
    one would read the bullet it meant as clean while the report said the scan ran.
    The scan fails closed and the detectors decide, exactly as with the scan off: the
    flag it did name buys no call."""
    sel, bullets = _two_items()
    rec, result = _sweep(monkeypatch, dict(bullets), sel=sel, scan=scan, first=_answer())
    assert rec.kinds == ["scan"]
    assert result.scan == sweep.SCAN_FAILED
    assert result.gate == sweep.GATE_DETECTORS
    assert result.skipped == (ITEM, "Trailhead") and result.judge_flags == ()


def test_a_verbatim_gkey_in_the_answer_is_dropped_quietly():
    """A verbatim bullet is never sent and never rewritten, so naming it changes
    nothing and is no sign of a misread answer."""
    vgk = compose._VERBATIM_PREFIX + "/Example Corp/0"
    got = sweep._parse_scan({"flags": [{"gkey": vgk, "tells": ["filler"]},
                                       {"gkey": "a2", "tells": ["hype words"]}]},
                            ["a1", "a2"])
    assert got == {"a1": (), "a2": ("hype words",)}


def test_a_bare_empty_array_is_a_clean_scan(engine, monkeypatch):
    """The clean answer with its wrapper dropped. Read as a failure it would buy every
    item its call, which is the case the scan exists to make cheap."""
    rec, result = _sweep(monkeypatch, dict(CLEAN), scan=[])
    assert rec.kinds == ["scan"]
    assert result.scan == sweep.SCAN_RAN
    assert result.skipped == (ITEM,)


def test_the_closing_names_the_clean_answer():
    assert '{"flags": []}' in sweep._SCAN_CLOSING


def test_a_padded_gkey_still_names_its_bullet():
    got = sweep._parse_scan({"flags": [{"gkey": " a2 ", "tells": ["filler"]}]}, ["a1", "a2"])
    assert got == {"a1": (), "a2": ("filler",)}


@pytest.mark.parametrize("answer", [
    {"flags": ["a2"]},
    {"flags": [{"gkey": "a2", "tell": "hype words"}]},
    {"flags": [{"gkey": "zz", "tells": ["hype words"]}]},
    {"flags": [{"gkey": "a2", "tells": "hype words"}]},
    {"flags": [{"gkey": "a2", "tells": {}}]},
    {"flags": [{"gkey": None, "tells": ["hype words"]}]},
    {"flags": [{"gkey": "a1", "tells": ["filler"]}, {"gkey": "zz", "tells": ["hype"]}]},
], ids=["bare-gkeys", "renamed-key", "unknown-gkeys-only", "tells-string",
        "tells-object", "gkey-null", "one-good-one-unknown"])
def test_a_malformed_flag_parses_to_none(answer):
    """One flag the parser cannot use makes the answer a misread shape, never a clean
    resume: dropping it would skip the call that flag asked for."""
    assert sweep._parse_scan(answer, ["a1", "a2"]) is None


def test_parse_maps_every_sent_bullet():
    got = sweep._parse_scan(_flags(a2=["hype words"]), ["a1", "a2", "a3"])
    assert got == {"a1": (), "a2": ("hype words",), "a3": ()}


@pytest.mark.parametrize("answer", [
    None, "garbage", 7, ["not", "objects"], {"bullets": []}, {"flags": "hype"},
    {"flags": None},
], ids=["none", "string", "number", "list", "no-flags-key", "flags-string",
        "flags-null"])
def test_a_malformed_answer_parses_to_none(answer):
    assert sweep._parse_scan(answer, ["a1"]) is None


# ── failure ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("scan", [
    RuntimeError("transport down"), "garbage", ["not", "objects"], {"bullets": []},
    {"flags": "hype"}, {"flags": ["a1"]},
], ids=["raises", "non-dict", "list", "no-flags-key", "flags-not-a-list",
        "no-usable-flag"])
def test_a_failed_scan_leaves_the_detectors_to_decide(engine, detector_gate, monkeypatch,
                                                      scan):
    """A detector hit in one entry buys that entry the call it buys with the scan off,
    byte for byte; the clean entry makes none."""
    sel, bullets = _two_items()
    bullets["a1"] = item_sweep._FORMULAIC
    monkeypatch.setenv(SCAN_ENV, "0")
    off, _r = _sweep(monkeypatch, dict(bullets), sel=sel, first=_answer())
    monkeypatch.delenv(SCAN_ENV)
    rec, result = _sweep(monkeypatch, dict(bullets), sel=sel, scan=scan, first=_answer())
    assert off.kinds == ["sweep"]
    assert rec.kinds == ["scan", "sweep"]
    assert rec.calls[1:] == off.calls
    assert result.scan == sweep.SCAN_FAILED
    assert result.gate == sweep.GATE_DETECTORS
    assert result.skipped == ("Trailhead",) and result.judge_flags == ()
    assert (result.calls, result.items) == (2, 1)


# ── with Jev ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("judge", [Reads(), Reads({CLEAN["a2"]: ("hype",)})],
                         ids=["clean-read", "flagged-read"])
def test_with_jev_reading_there_is_no_scan(engine, monkeypatch, judge):
    rec, result = _sweep(monkeypatch, dict(CLEAN), scan=None, first=_answer(),
                         judge=judge)
    assert "scan" not in rec.kinds
    assert result.scan == ""


def test_a_failed_jev_read_falls_back_to_the_scan(engine, monkeypatch):
    rec, result = _sweep(monkeypatch, dict(CLEAN), scan=_CLEAN_SCAN, judge=Failing())
    assert rec.kinds == ["scan"]
    assert result.scan == sweep.SCAN_RAN
    assert result.skipped == (ITEM,)


# ── the toggle off ──────────────────────────────────────────────────────────
def test_with_the_toggle_off_there_is_no_scan_and_a_detector_call_is_the_plain_one(
        engine, detector_gate, monkeypatch):
    """No scan, and the entry a detector names is sent with the plain system prompt
    and no `judge_flags` key; the clean entry makes no call."""
    monkeypatch.setenv(SCAN_ENV, "0")
    sel, bullets = _two_items()
    bullets["a1"] = item_sweep._FORMULAIC
    rec, result = _sweep(monkeypatch, bullets, sel=sel, scan=None, first=_answer())
    assert rec.kinds == ["sweep"]
    assert all(system == sweep._SWEEP_SYSTEM for _k, system, _u in rec.calls)
    assert all("judge_flags" not in user for _k, _s, user in rec.calls)
    assert all(sweep._SWEEP_FLAG_CLOSING not in user for _k, _s, user in rec.calls)
    assert result.scan == ""
    assert result.skipped == ("Trailhead",)
    assert (result.calls, result.items) == (1, 1)


def test_with_the_toggle_off_a_failed_jev_read_leaves_the_detectors(engine, detector_gate,
                                                                    monkeypatch):
    monkeypatch.setenv(SCAN_ENV, "0")
    rec, result = _sweep(monkeypatch, dict(CLEAN), scan=None, judge=Failing())
    assert rec.calls == []
    assert result.scan == ""
    assert result.gate == sweep.GATE_DETECTORS
    assert result.skipped == (ITEM,)


# ── the prompt ──────────────────────────────────────────────────────────────
def test_the_scan_prompt_carries_the_rules_the_repair_carries():
    system = sweep._SCAN_SYSTEM
    assert compose.BANNED_PHRASING in system
    assert aiwriting.RESUME_RULES_PROMPT in system
    for name in jev_assist.SWEEP_QUESTIONS:
        assert name in system


def test_the_scan_prompt_says_resume_grammar_is_no_tell():
    system = sweep._SCAN_SYSTEM.lower()
    assert "subjectless" in system
    assert "action verb" in system


def test_the_scan_prompt_names_tells_and_never_rewrites():
    assert "Never rewrite" in sweep._SCAN_SYSTEM
    assert '"flags"' in sweep._scan_user_prompt({"entries": []})


def test_the_scan_prompt_is_free_of_the_characters_it_bans():
    for text in (sweep._SCAN_SYSTEM, sweep._SCAN_CLOSING):
        assert chr(0x2014) not in text
        assert " -- " not in text


def test_the_scan_prompt_is_free_of_the_phrasing_it_bans():
    """The ban enumerations it embeds are exempt (a list cannot forbid a phrase
    without quoting it), so only this module's own words are checked."""
    own = sweep._SCAN_SYSTEM
    for enumeration in (compose.BANNED_PHRASING, aiwriting.RESUME_RULES_PROMPT):
        own = own.replace(enumeration, "")
    assert compose.style_violations(own) == []
    assert compose.style_violations(sweep._SCAN_CLOSING) == []


# ── the report ──────────────────────────────────────────────────────────────
def _report(result):
    report = rt_run.RunLog()
    ctx = rt_run.PassCtx(jd="", job_title="Data Engineer", sel=_sel(), bullets={},
                         verbatim={}, reserved=frozenset(), log=lambda _m: None,
                         report=report)
    rt_run._report_sweep(ctx, result, stage=rt_run.AIWRITING_SWEEP_STAGE)
    return report.note_lines


def _result(**kw):
    base = dict(changed=(), rejected=(), reasked=(), unfixed_p2=(), unfixed_phrasing=(),
                calls=2, items=1, failures=())
    base.update(kw)
    return sweep.SweepResult(**base)


def test_the_report_credits_the_scan_and_never_jev():
    lines = _report(_result(scan=sweep.SCAN_RAN, skipped=("Trailhead",),
                            judge_flags=(sweep.JudgeFlag("a2", ITEM, ("hype words",)),)))
    text = "\n".join(lines)
    assert "Jev" not in text
    assert ("the résumé scan skipped the call for 1 item(s) with no tell and no "
            "detector finding: Trailhead") in text
    assert ("the résumé scan read hype words in bullet 'a2' in 'Example Corp', so that "
            "item was sent to the sweep") in text
    assert "the résumé scan read 2 item(s) in one call and sent 1 to repair" in text
    assert ("swept 1 item(s) in 2 call(s) (one of them the résumé scan) and rewrote 0 "
            "bullet(s)") in text


def test_a_failed_scan_is_reported_as_a_note():
    lines = _report(_result(scan=sweep.SCAN_FAILED, gate=sweep.GATE_DETECTORS, calls=2,
                            items=1, skipped=("Trailhead",)))
    text = "\n".join(lines)
    assert ("the résumé scan failed, so the detectors alone chose which items to "
            "repair") in text
    assert ("no detector found anything to repair in 1 item(s), so they made no call "
            "(with no read, a call returns such bullets unchanged): Trailhead") in text
    assert "the résumé scan skipped" not in text


def test_a_skip_with_no_read_is_credited_to_no_read():
    text = "\n".join(_report(_result(gate=sweep.GATE_DETECTORS, calls=0, items=0,
                                     skipped=(ITEM, "Trailhead"))))
    assert "no detector found anything to repair in 2 item(s)" in text
    assert "Jev" not in text and "résumé scan" not in text


def test_the_jev_lines_are_unchanged_without_a_scan():
    lines = "\n".join(_report(_result(skipped=("Trailhead",), judge_flags=(
        sweep.JudgeFlag("a2", ITEM, ("hype words",)),))))
    assert "Jev's sweep gate skipped the call for 1 item(s)" in lines
    assert "Jev read hype words in bullet 'a2'" in lines
    assert "swept 1 item(s) in 2 call(s) and rewrote 0 bullet(s)" in lines
    assert "résumé scan" not in lines


def test_the_pass_logs_what_the_scan_sent(engine, monkeypatch):
    sel, bullets = _two_items()
    _install(monkeypatch, ScanRecorder(scan=_flags(v1=["filler"]), first=_answer()))
    logged = []
    ctx = rt_run.PassCtx(jd="", job_title="Data Engineer", sel=sel, bullets=bullets,
                         verbatim={}, reserved=frozenset(), log=logged.append,
                         report=rt_run.RunLog())
    rt_run._pass_aiwriting_sweep(ctx)
    assert ("AI-writing sweep: the résumé scan ran and sent 1 of 2 item(s) to repair."
            in logged)
