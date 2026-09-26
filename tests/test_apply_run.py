"""`apply_run`: the drain loop over the local HTML fixtures, `FakeJev` and a
temp queue (headless Chromium through the module-scoped test browser; the
runner gets the test context injected so nothing holds a window). The
synthetic apply.md comes from `apply_data.build_markdown` over a synthetic
master, as `test_apply_facts.py` does; no real store, profile or site is
touched. Skips without Playwright or Chromium. No `asyncio.run()` here (see
`conftest_browser`)."""
import json
import re
import sys
from pathlib import Path

import pytest

pytest.importorskip("playwright")

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import apply_facts  # noqa: E402
import apply_form  # noqa: E402
import apply_judge  # noqa: E402
import apply_queue  # noqa: E402
import apply_run  # noqa: E402
import ats_accounts  # noqa: E402
import jev  # noqa: E402
import apply_harness as h  # noqa: E402
import jev_harness  # noqa: E402
from answer_bank import standard_bank, unconfirmed  # noqa: E402
from apply_judge import FillPlan, PlannedField, VerifyResult  # noqa: E402
from resume_tailor import apply_answers, apply_config, apply_data  # noqa: E402

pytest_plugins = ["conftest_browser", "conftest_jev"]

_PDF = (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[]/Count 0>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n")

_MASTER = {
    "basics": {"name": "Jane Doering", "email": "jane.doe@example.com",
               "phone": "555-555-0100", "location": "Anytown, CA",
               "linkedin": "https://linkedin.com/in/janedoe",
               "github": "https://github.com/janedoe"},
    "education": [{"school": "State University", "degree": "B.S.",
                   "concentration": "Computer Science", "dates": "2020 - 2024"}],
    "experience": [
        {"org": "Acme Corp", "title": "Software Engineer", "location": "Anytown, CA",
         "dates": "2024-06 / present", "achievements": [{"id": "a1"}]},
    ],
    "projects": [],
    "leadership": [],
}
_JOB = {"job_posting_id": "42", "company_name": "Fabrikam", "job_title": "Analytics Engineer",
        "url": "https://example.com/job/42"}
_SEL = {"experience": [{"name": "Acme Corp", "groups": [["a1"]]}]}
_BULLETS = {"a1": "Built the ingestion pipeline."}

_RUN_CONTEXT = {"signup_email": "jane.doe@example.com",
                "inbox_url": "https://mail.example.com/inbox"}


def _bank():
    """The shared confirmed answers (`answer_bank`)."""
    return standard_bank()


@pytest.fixture(autouse=True)
def _fast_timing():
    """The fixtures are static pages: the harness's short settle and click
    windows (`apply_harness.FAST_TIMING`) read them as well as the
    production ones. A test whose page moves on by a timer sets its own
    quiet window."""
    import apply_harness
    with apply_harness.fast_timing():
        yield


@pytest.fixture(autouse=True)
def _hermetic(tmp_path, monkeypatch, jev_judge):
    monkeypatch.setattr(ats_accounts, "_get_master_password", lambda: None)
    monkeypatch.setenv("ATS_ACCOUNTS_PATH", str(tmp_path / "accounts.json"))
    monkeypatch.setattr(apply_config, "APPLY_CONFIG", tmp_path / "missing.json")
    monkeypatch.setattr(apply_answers, "STORE_PATH", tmp_path / "apply_answers.json")
    # the runner reads its answers from the store (FL-1, FL-4): a confirmed one
    apply_answers.save(_bank())
    monkeypatch.setenv("APPLY_QUEUE_PATH", str(tmp_path / "apply_queue.json"))


@pytest.fixture
def catalog_builder(monkeypatch):
    """`apply_facts.build` with the synthetic bank when a caller passes none
    (the runner passes the store it read, which `_hermetic` seeds with the
    same bank)."""
    real = apply_facts.build

    def _build(folder, **kw):
        kw.setdefault("answers", _bank())
        return real(folder, **kw)
    monkeypatch.setattr(apply_run.apply_facts, "build", _build)
    return _build


@pytest.fixture
def job_folder(tmp_path):
    folder = tmp_path / "job42"
    folder.mkdir()
    md = apply_data.build_markdown(_MASTER, _JOB, _bank(), sel=_SEL, bullets=_BULLETS,
                                   cover_body="Dear hiring team,\n\nI am writing to apply.")
    (folder / "apply.md").write_text(md, encoding="utf-8")
    (folder / "Jane_Doe_Resume.pdf").write_bytes(_PDF)
    return folder


@pytest.fixture
def context(_browser):
    ctx = _browser.new_context()
    try:
        yield ctx
    finally:
        ctx.close()


def test_this_modules_browser_context_is_offline(context):
    # every context of the browser tests has the offline guard (the
    # conftest's `offline_contexts`); 192.0.2.1 is a documentation address
    page = context.new_page()
    with pytest.raises(Exception, match="ERR_BLOCKED_BY_CLIENT"):
        page.goto("http://192.0.2.1/", timeout=4_000)


def test_a_routes_own_fetch_in_this_modules_browser_is_offline(context):
    # a route's fetch passes no route (the run fetches an emailed link's
    # pages so): the conftest holds it to the local hosts too
    page = context.new_page()
    failed: list[str] = []

    def _fetch(route):
        try:
            route.fetch(max_redirects=0, timeout=4_000)
        except Exception as e:  # noqa: BLE001
            failed.append(str(e))
        route.abort()
    page.route("**/*", _fetch)
    with pytest.raises(Exception):
        page.goto("http://192.0.2.1/", timeout=4_000)
    assert failed == ["offline: a route's fetch off the local hosts"]


def _enqueue(job_folder, url, jid="42", **kw):
    e = apply_queue.new_entry(jid, company="Fabrikam", title="Analytics Engineer",
                              apply_url=url, **kw)
    apply_queue.enqueue(e)
    apply_queue.set_artifacts(jid, {"folder": str(job_folder),
                                    "apply_md": str(job_folder / "apply.md"),
                                    "resume_pdf": str(job_folder / "Jane_Doe_Resume.pdf")})
    return apply_queue.load()["jobs"][-1]


def _no_sleep(seconds):
    """Every runner here sleeps for no time at all: an inbox poll or a finish
    retry that waits its real 60 s would stall a record or replay run past the
    pytest timeout when the live judge reads a fixture differently."""


def _runner(context, tmp_path, **settings):
    base = {"auto_apply_submit": True, "auto_apply_headless": True,
            "auto_apply_jev_mode": "fake", "auto_apply_batch_cap": 10,
            "auto_apply_generate": True}
    base.update(settings)
    return apply_run.Runner(jev=jev_harness.judge(), profile_dir=tmp_path / "profile",
                            settings=base, context=context, run_context=_RUN_CONTEXT,
                            sleep=_no_sleep)


def _entry(jid="42"):
    return next(e for e in apply_queue.load()["jobs"] if e["job_posting_id"] == jid)


# --- (a) the Ashby multi-step form runs to confirmation and finishes submitted -------

def test_ashby_steps_runs_to_confirmation_and_finishes_submitted(
        context, fixture_url, job_folder, catalog_builder, tmp_path):
    _enqueue(job_folder, fixture_url("ashby_steps.html"))
    runner = _runner(context, tmp_path)
    outcomes = runner.drain(cap=5)
    assert len(outcomes) == 1
    out = outcomes[0]
    assert out.status == "submitted", out
    assert out.reason == "confirmation page"
    assert out.job_id == "42"
    entry = _entry()
    assert entry["status"] == "submitted"
    assert entry["artifacts"]["application_record"] == str(out.record_path)
    record = Path(out.record_path).read_text(encoding="utf-8")
    for label, value in (("First name", "Jane"), ("Last name", "Doering"),
                         ("Email", "jane.doe@example.com"), ("Phone", "555-555-0100"),
                         ("Are you authorized to work in the US?", "Yes"),
                         ("Will you now or in the future require sponsorship?", "No")):
        assert f"{label}: {value}" in record, (label, record)
    assert "SUBMIT CLICKED" in record
    assert re.search(r"^- Requests: \d+$", record, re.M), record
    assert re.search(r"^- Input tokens: \d+$", record, re.M), record
    assert jev.MODEL in record
    assert out.pages >= 4
    # the optional essay had no generator: skipped, recorded, and no bar to the submit
    assert [m["question"] for m in entry["missing_answers"]] == [
        "Why do you want to work here? Tell us about your motivation for this role."]
    assert "- Why do you want to work here? Tell us about your motivation for this role." in record
    assert context.pages == [] or all(p.is_closed() for p in context.pages)


# --- (b) auto_apply_submit off: ready_to_submit, submit never clicked ------------------

def test_submit_off_finishes_ready_to_submit_and_never_clicks_submit(
        context, fixture_url, job_folder, catalog_builder, tmp_path):
    _enqueue(job_folder, fixture_url("ashby_steps.html"))
    out = _runner(context, tmp_path, auto_apply_submit=False).drain(cap=1)[0]
    assert out.status == "ready_to_submit", out
    assert out.reason == "auto_apply_submit is off"
    entry = _entry()
    assert entry["status"] == "ready_to_submit"
    assert entry["tab_note"] == apply_run.REVIEW_NOTE
    page = next(p for p in context.pages if not p.is_closed())     # the tab stays open
    assert page.locator("body[data-submitted]").count() == 0
    assert page.locator("#why").is_visible()                        # parked on the last step
    record = Path(out.record_path).read_text(encoding="utf-8")
    assert "SUBMIT CLICKED" not in record


def test_a_quiet_submit_is_clicked_exactly_once_and_the_late_confirmation_is_read(
        context, fixture_url, job_folder, catalog_builder, tmp_path, monkeypatch):
    # the fixture's submit changes nothing for 3 s; click_button's window is cut
    # to 1 s so the click reads as "quiet", which is no proof it failed
    monkeypatch.setattr(apply_run, "CLICK_TIMEOUT_S", 1)
    _enqueue(job_folder, fixture_url("slow_submit.html"))
    out = _runner(context, tmp_path).drain(cap=1)[0]
    assert out.status == "submitted" and out.reason == "confirmation page", out
    page = next(p for p in context.pages if not p.is_closed()) if any(
        not p.is_closed() for p in context.pages) else None
    assert page is None                                   # submitted: the page was closed
    record = Path(out.record_path).read_text(encoding="utf-8")
    assert record.count("SUBMIT CLICKED") == 1
    assert "State: confirmation" in record


def test_native_submit_continue_advances_before_final_submit(
        context, fixture_url, job_folder, catalog_builder, tmp_path):
    _enqueue(job_folder, fixture_url("native_submit_steps.html"))
    out = _runner(context, tmp_path).drain(cap=1)[0]
    assert out.status == "submitted", out
    assert out.reason == "confirmation page"
    record = Path(out.record_path).read_text(encoding="utf-8")
    assert "Continue (advance)" in record
    assert "Submit application (submit)" in record
    assert "State: confirmation" in record


def test_submit_click_is_never_retried(context, fixture_url, job_folder, catalog_builder,
                                       tmp_path, monkeypatch):
    monkeypatch.setattr(apply_run, "CLICK_TIMEOUT_S", 1)
    clicks = []
    real = apply_run.apply_fill.click

    def _counting(page, digest, n, **kw):
        got = real(page, digest, n, **kw)
        clicks.append((next(b.text for b in digest.buttons if b.n == n), got.clicked,
                       got.changed, page.evaluate("window.__clicks")))
        return got
    monkeypatch.setattr(apply_run.apply_fill, "click", _counting)
    _enqueue(job_folder, fixture_url("slow_submit.html"))
    out = _runner(context, tmp_path).drain(cap=1)[0]
    assert clicks == [("Submit", True, False, 1)]     # one click; landed; quiet; no second click
    assert out.status == "submitted" and out.reason == "confirmation page"


def test_a_submit_click_that_never_lands_parks_ready_to_submit_without_a_post_submit_judge(
        context, fixture_url, job_folder, catalog_builder, tmp_path, monkeypatch):
    import apply_form
    from playwright.sync_api import TimeoutError as PWTimeout
    real = apply_form.resolve

    class _Raising:
        @property
        def first(self):
            return self

        def count(self):
            return 1

        def click(self, **kw):
            raise PWTimeout("Timeout 5000ms exceeded")

    monkeypatch.setattr(apply_run.apply_fill.apply_form, "resolve",
                        lambda page, loc: _Raising() if loc[1] == "#btn-submit"
                        else real(page, loc))

    def _no_post_submit(self):
        raise AssertionError("the post-submit judge ran")
    monkeypatch.setattr(apply_run._JobRun, "_after_submit", _no_post_submit)
    _enqueue(job_folder, fixture_url("slow_submit.html"))
    out = _runner(context, tmp_path).drain(cap=1)[0]
    assert out.status == "ready_to_submit", out
    assert out.reason == "submit did not register"
    entry = _entry()
    assert entry["tab_note"] == apply_run.SUBMIT_FAILED_NOTE ==         "submit did not register; review and submit"
    page = next(p for p in context.pages if not p.is_closed())
    assert page.locator("body[data-submitted]").count() == 0
    record = Path(out.record_path).read_text(encoding="utf-8")
    assert "SUBMIT CLICKED" not in record and "submit did not register" in record


class _AdvanceJudge(jev.FakeJev):
    """The fake, with every "Apply now" button judged `advance`."""

    def judge(self, state, questions):
        out = super().judge(state, questions)
        for i, b in enumerate(state.get("buttons") or []):
            if b.get("text") == "Apply now" and f"button_{b['n']}_role" in out:
                out[f"button_{b['n']}_role"] = jev.Answer(
                    kind="choice", choice="advance", probabilities={"advance": 1.0},
                    confidence=1.0)
        return out


def test_a_submit_shaped_button_judged_advance_goes_to_the_gate_unclicked(
        context, fixture_url, job_folder, catalog_builder, tmp_path):
    _enqueue(job_folder, fixture_url("apply_now_form.html"))
    runner = _runner(context, tmp_path, auto_apply_submit=False)
    runner.jev = _AdvanceJudge()
    out = runner.drain(cap=1)[0]
    assert out.status == "ready_to_submit", out
    assert out.reason == "auto_apply_submit is off"
    page = next(p for p in context.pages if not p.is_closed())
    assert page.evaluate("window.__clicks") == 0
    assert page.locator("body[data-submitted]").count() == 0
    assert page.locator("#first_name").input_value() == "Jane"
    record = Path(out.record_path).read_text(encoding="utf-8")
    assert "(advance)" not in record


# --- (c) the captcha page parks needs_human with the flag in the notes ----------------

def test_captcha_page_parks_needs_human_with_the_flag(
        context, fixture_url, job_folder, catalog_builder, tmp_path):
    _enqueue(job_folder, fixture_url("captcha.html"))
    out = _runner(context, tmp_path).drain(cap=1)[0]
    assert out.status == "needs_human", out
    entry = _entry()
    assert "captcha" in entry["notes"].lower()
    flag = re.search(r"has_captcha p=(\d\.\d\d)", entry["notes"])
    assert flag and float(flag.group(1)) > 0.5, entry["notes"]
    assert entry["tab_note"].startswith("http://127.0.0.1:")
    assert out.pages == 1


# --- (d) a required essay with generation off parks and records the question ---------

def test_required_essay_with_generation_off_parks_and_records_the_question(
        context, fixture_url, job_folder, catalog_builder, tmp_path):
    _enqueue(job_folder, fixture_url("essay_required.html"))
    out = _runner(context, tmp_path, auto_apply_generate=False).drain(cap=1)[0]
    assert out.status == "needs_human", out
    assert out.reason == "required field without an answer: Describe a project you are proud of and your motivation for this role"
    entry = _entry()
    questions = [m["question"] for m in entry["missing_answers"]]
    assert "Describe a project you are proud of and your motivation for this role" in questions
    assert "Max 1500 characters." in entry["missing_answers"][0]["context"]
    page = next(p for p in context.pages if not p.is_closed())
    assert page.locator("body[data-submitted]").count() == 0
    record = Path(out.record_path).read_text(encoding="utf-8")
    assert "Describe a project you are proud of and your motivation for this role (Max 1500 characters.)" in record


def test_generation_on_uses_the_answergen_hook_and_fills_the_essay(
        context, fixture_url, job_folder, catalog_builder, tmp_path):
    class Gen:
        calls = []

        def answer(self, field, catalog, judge, *, budget):
            self.calls.append((field.label, budget))
            return "Built the ingestion pipeline at Acme Corp."

    _enqueue(job_folder, fixture_url("essay_required.html"))
    runner = _runner(context, tmp_path)
    runner.answergen = Gen()
    out = runner.drain(cap=1)[0]
    assert Gen.calls == [("Describe a project you are proud of and your motivation for this role", apply_run.GENERATE_MAX)]
    record = Path(out.record_path).read_text(encoding="utf-8")
    assert "Describe a project you are proud of and your motivation for this role: Built the ingestion pipeline at Acme Corp." in record
    assert _entry()["missing_answers"] == []


def test_a_generated_answer_is_verified_against_its_draft_in_code_and_the_sheet_check_covers_typed_facts(
        context, fixture_url, job_folder, catalog_builder, tmp_path):
    """The live judge read "is `filled_value` the correct value according to
    `sheet_excerpt`" at 0.05 to 0.20 for a generated essay, since the sheet
    holds no essay: the grounding gate is that draft's check, and what is left
    to verify is that the typed text is the draft, which is a string
    comparison. No verify or placeholder question carries the draft."""
    draft = "Built the ingestion pipeline at Acme Corp."
    asked = []

    class Judge(jev.FakeJev):
        def judge(self, state, questions):
            for qid, q in questions.items():
                if qid.startswith(("verify_", "placeholder_")):
                    asked.append(json.dumps({"state": state, "q": q}))
            return super().judge(state, questions)

    class Gen:
        def answer(self, field, catalog, judge, *, budget):
            return draft

    _enqueue(job_folder, fixture_url("essay_required.html"))
    runner = _runner(context, tmp_path)
    runner.jev = Judge()
    runner.answergen = Gen()
    out = runner.drain(cap=1)[0]
    assert out.status == "submitted", out
    assert asked and not any(draft in blob for blob in asked)
    record = Path(out.record_path).read_text(encoding="utf-8")
    assert ("  - Describe a project you are proud of and your motivation for this role: "
            "ok (p_correct 1.00, p_placeholder 0.00)") in record, record
    assert "  - First name: ok (p_correct 0.90" in record         # the rest still go to the judge


# --- SP6: the real generator behind the hook, drafts mocked, FakeJev grounding ------------

_GROUNDED_DRAFT = "Built the ingestion pipeline at Acme Corp."
_UNGROUNDED_DRAFT = ("Built the ingestion pipeline at Acme Corp. "
                     "Baking sourdough bread relaxes me on weekends.")
_ESSAYS_FOUR = ("Tell us about your motivation for this role",
                "Describe a project you are proud of and your motivation for this role",
                "What about this role matches your motivation as a candidate",
                "Anything else about your motivation for this role")


_GROUNDED_RE = re.compile(r"weakest sentence grounded (\d\.\d\d), below (\d\.\d\d)")


def _grounded_in(text):
    """The weakest grounding probability a rejection note names. The fake
    says 0.10 and the live judge its own number; the test checks the gate."""
    m = _GROUNDED_RE.search(text)
    assert m, text
    assert float(m.group(2)) == apply_judge.GROUNDING_MIN
    return float(m.group(1))


def _rejected_line(label, record):
    line = next((ln for ln in record.splitlines()
                 if ln.startswith(f"  - {label}: rejected (draft rejected: ")), None)
    assert line, (label, record)
    return _grounded_in(line)


def _generator(text):
    import apply_answergen
    calls = []

    def fake_call(system, user, tier, **kw):
        calls.append((user, tier))
        return text
    return apply_answergen.Generator(llm_call=fake_call), calls


def test_a_rejected_draft_leaves_an_optional_field_blank_and_flags_it_in_the_record(
        context, fixture_url, job_folder, catalog_builder, tmp_path):
    _enqueue(job_folder, fixture_url("essays_four.html"))
    runner = _runner(context, tmp_path)
    runner.answergen, calls = _generator(_UNGROUNDED_DRAFT)
    out = runner.drain(cap=1)[0]
    assert out.status == "submitted", out
    assert len(calls) == apply_run.GENERATE_MAX          # one draft per field, three at most
    assert all(t == "flash_lite" for _, t in calls)
    page = next(p for p in context.pages if not p.is_closed()) if any(
        not p.is_closed() for p in context.pages) else None
    assert page is None                                   # submitted: the tab was closed
    record = Path(out.record_path).read_text(encoding="utf-8")
    assert "- Generated answers:" in record
    for label in _ESSAYS_FOUR[:3]:
        assert _rejected_line(label, record) < apply_judge.GROUNDING_MIN, record
    assert f"  - {_ESSAYS_FOUR[3]}: rejected (generation budget exhausted)" in record
    assert "(generated)" not in record
    assert "- Generated answers used: 0" in record
    assert out.jev_usage["generated"] == 0
    questions = [m["question"] for m in _entry()["missing_answers"]]
    assert questions == list(_ESSAYS_FOUR)


def test_a_rejected_draft_parks_a_required_field_with_the_grounding_note(
        context, fixture_url, job_folder, catalog_builder, tmp_path):
    _enqueue(job_folder, fixture_url("essay_required.html"))
    runner = _runner(context, tmp_path)
    runner.answergen, calls = _generator(_UNGROUNDED_DRAFT)
    out = runner.drain(cap=1)[0]
    assert out.status == "needs_human", out
    assert out.reason.startswith("required field without an answer: Describe a project you are "
                                 "proud of and your motivation for this role; draft rejected: "
                                 "weakest sentence grounded 0."), out.reason
    assert _grounded_in(out.reason) < apply_judge.GROUNDING_MIN
    assert len(calls) == 1
    page = next(p for p in context.pages if not p.is_closed())
    assert page.locator("body[data-submitted]").count() == 0
    assert page.locator("#project").input_value() == ""
    record = Path(out.record_path).read_text(encoding="utf-8")
    assert _rejected_line("Describe a project you are proud of and your motivation for this role",
                          record) < apply_judge.GROUNDING_MIN, record


def test_at_most_three_drafts_per_job_and_generated_answers_are_marked(
        context, fixture_url, job_folder, catalog_builder, tmp_path):
    _enqueue(job_folder, fixture_url("essays_four.html"))
    runner = _runner(context, tmp_path)
    runner.answergen, calls = _generator(_GROUNDED_DRAFT)
    out = runner.drain(cap=1)[0]
    assert out.status == "submitted", out
    assert len(calls) == 3
    assert out.jev_usage["generated"] == 3
    record = Path(out.record_path).read_text(encoding="utf-8")
    for label in _ESSAYS_FOUR[:3]:
        assert f"  - {label}: {_GROUNDED_DRAFT} (generated)" in record, record
        assert f"  - {label}: generated (" in record
    assert f"  - {_ESSAYS_FOUR[3]}: rejected (generation budget exhausted)" in record
    assert "- Generated answers used: 3" in record
    assert [m["question"] for m in _entry()["missing_answers"]] == [_ESSAYS_FOUR[3]]


def test_a_hook_without_a_last_attempt_records_no_generator(
        context, fixture_url, job_folder, catalog_builder, tmp_path):
    class Gen:
        def answer(self, field, catalog, judge, *, budget):
            return None

    _enqueue(job_folder, fixture_url("essay_required.html"))
    runner = _runner(context, tmp_path)
    runner.answergen = Gen()
    out = runner.drain(cap=1)[0]
    assert out.status == "needs_human", out
    assert out.reason == ("required field without an answer: Describe a project you are proud "
                          "of and your motivation for this role")
    record = Path(out.record_path).read_text(encoding="utf-8")
    assert ("  - Describe a project you are proud of and your motivation for this role: "
            "rejected (no generator)") in record


def test_main_wires_the_generator_when_auto_apply_generate_is_on(hermetic_cli, monkeypatch):
    import apply_answergen
    monkeypatch.setattr(apply_run.jev, "get", lambda mode="": jev_harness.judge())
    seen = {}

    class R:
        def __init__(self, **kw):
            seen.update(kw)

        def drain(self, cap):
            return []
    monkeypatch.setattr(apply_run, "Runner", R)
    assert apply_run.main(["drain"]) == 0
    assert isinstance(seen["answergen"], apply_answergen.Generator)
    monkeypatch.setattr(apply_run, "load_settings",
                        lambda: {**apply_run.DEFAULT_SETTINGS, "auto_apply_generate": False})
    assert apply_run.main(["drain"]) == 0
    assert isinstance(seen["answergen"], apply_run.NotConfigured)


# --- (e) MAX_PAGES exhaustion parks -------------------------------------------------

def test_max_pages_exhaustion_parks(context, fixture_url, job_folder, catalog_builder,
                                    tmp_path, monkeypatch):
    monkeypatch.setattr(apply_judge, "MAX_PAGES", 1)
    _enqueue(job_folder, fixture_url("ashby_steps.html"))
    out = _runner(context, tmp_path).drain(cap=1)[0]
    assert out.status == "needs_human", out
    assert out.reason == "page budget exhausted (1 pages; last: application_form)"
    assert out.pages == 1
    page = next(p for p in context.pages if not p.is_closed())
    assert page.locator("body[data-submitted]").count() == 0
    assert page.locator("#first_name").input_value() == "Jane"      # step 1 was filled


def test_wall_clock_exhaustion_parks(context, fixture_url, job_folder, catalog_builder,
                                     tmp_path):
    ticks = iter([0.0, 0.0, apply_run.JOB_WALL_CLOCK_S + 1.0] + [10_000.0] * 50)
    _enqueue(job_folder, fixture_url("ashby_steps.html"))
    runner = apply_run.Runner(jev=jev_harness.judge(), profile_dir=tmp_path / "profile",
                              settings={"auto_apply_headless": True}, context=context,
                              run_context=_RUN_CONTEXT, clock=lambda: next(ticks),
                              sleep=_no_sleep)
    out = runner.drain(cap=1)[0]
    assert out.status == "needs_human" and out.reason.startswith("time budget exhausted (")


def test_wall_clock_passing_mid_fill_stops_the_fill_on_the_runner_clock(
        context, fixture_url, job_folder, catalog_builder, tmp_path):
    """The runner's clock reaches the deadline after the loop check, inside
    `apply_fill.apply`: nothing is typed, and the next loop check parks."""
    base = 1e9                     # far past time.monotonic(): only the injected clock can expire
    now = [base]

    def _clock():
        return now[0]
    _enqueue(job_folder, fixture_url("ashby_steps.html"))
    runner = apply_run.Runner(jev=jev_harness.judge(), profile_dir=tmp_path / "profile",
                              settings={"auto_apply_headless": True}, context=context,
                              run_context=_RUN_CONTEXT, clock=_clock, sleep=_no_sleep)
    real = apply_run.apply_fill.apply

    def _late(page, plan, **kw):
        now[0] = base + apply_run.JOB_WALL_CLOCK_S + 1.0     # the clock jumps during the fill
        return real(page, plan, **kw)
    runner_apply = apply_run.apply_fill
    orig = runner_apply.apply
    runner_apply.apply = _late
    try:
        out = runner.drain(cap=1)[0]
    finally:
        runner_apply.apply = orig
    assert out.status == "needs_human" and out.reason.startswith("time budget exhausted (")
    page = next(p for p in context.pages if not p.is_closed())
    assert page.locator("#first_name").input_value() == ""          # the fill saw the clock


# --- (f) drain claims FIFO until the queue is empty ----------------------------------

def test_drain_claims_fifo_until_the_queue_is_empty(
        context, fixture_url, job_folder, catalog_builder, tmp_path):
    _enqueue(job_folder, fixture_url("captcha.html"), jid="a")
    _enqueue(job_folder, fixture_url("essay_required.html"), jid="b")
    _enqueue(job_folder, fixture_url("captcha.html"), jid="c")
    apply_queue.enqueue(apply_queue.new_entry("t", apply_url=fixture_url("captcha.html"),
                                              status="tailoring"))
    outcomes = _runner(context, tmp_path, auto_apply_generate=False).drain(cap=10)
    assert [o.job_id for o in outcomes] == ["a", "b", "c"]
    assert [o.status for o in outcomes] == ["needs_human"] * 3
    statuses = {e["job_posting_id"]: e["status"] for e in apply_queue.load()["jobs"]}
    assert statuses == {"a": "needs_human", "b": "needs_human", "c": "needs_human",
                        "t": "tailoring"}
    assert apply_queue.claim(path=None) is None
    line = apply_run.summary_line(outcomes)
    assert line == ("drained 3: submitted 0, ready_to_submit 0, needs_human 3, failed 0; "
                    "Jev 0 requests, 0 tokens, $0.0000")


def test_drain_cap_stops_early_and_leaves_the_rest_queued(
        context, fixture_url, job_folder, catalog_builder, tmp_path):
    _enqueue(job_folder, fixture_url("captcha.html"), jid="a")
    _enqueue(job_folder, fixture_url("captcha.html"), jid="b")
    outcomes = _runner(context, tmp_path).drain(cap=1)
    assert [o.job_id for o in outcomes] == ["a"]
    assert _entry("b")["status"] == "queued"


def test_drain_on_an_empty_queue_returns_nothing(context, tmp_path):
    assert _runner(context, tmp_path).drain(cap=3) == []


# --- the missing sheet fails the entry without opening a page -------------------------

def test_missing_apply_md_finishes_failed(context, fixture_url, tmp_path, catalog_builder):
    e = apply_queue.new_entry("9", company="X", title="Y", apply_url=fixture_url("captcha.html"))
    apply_queue.enqueue(e)
    out = _runner(context, tmp_path).drain(cap=1)[0]
    assert out.status == "failed" and out.reason == "no apply.md"
    assert _entry("9")["status"] == "failed"
    assert out.pages == 0 and out.record_path == ""


# --- the allowlist ---------------------------------------------------------------------

def test_navigation_outside_the_allowlist_parks(context, fixture_url, job_folder,
                                                 catalog_builder, tmp_path):
    _enqueue(job_folder, fixture_url("ashby_steps.html"))
    apply_queue.update("42", ats={"domain": "jobs.example.com", "system": "greenhouse"})
    out = _runner(context, tmp_path).drain(cap=1)[0]
    assert out.status == "needs_human"
    assert out.reason == "left the allowed sites: 127.0.0.1"
    assert out.pages == 0                                 # parked before any navigation
    assert all(p.is_closed() for p in context.pages) or context.pages == []


def test_allowlist_carries_linkedin_the_ats_host_and_the_inbox_host(
        context, job_folder, tmp_path, catalog_builder):
    e = apply_queue.new_entry("42", apply_url="https://boards.greenhouse.io/acme/jobs/1")
    apply_queue.enqueue(e)
    run = apply_run._JobRun(_runner(context, tmp_path), context, e)
    run._build_allowlist()
    assert run.allowed == {"linkedin.com", "www.linkedin.com", "boards.greenhouse.io",
                           "mail.example.com"}


# --- the login wall with the default hook ---------------------------------------------

def test_login_wall_parks_with_the_login_note_by_default(
        context, fixture_url, job_folder, catalog_builder, tmp_path):
    _enqueue(job_folder, fixture_url("login_wall.html"))
    out = _runner(context, tmp_path).drain(cap=1)[0]
    assert out.status == "needs_human", out
    assert out.reason.startswith("login wall (read as login_wall "), out
    entry = _entry()
    assert entry["tab_note"] == apply_run.LOGIN_NOTE == "log in manually, then Re-queue"
    record = Path(out.record_path).read_text(encoding="utf-8")
    assert "State: login_wall" in record


def test_login_hook_that_signs_in_continues_the_loop(
        context, fixture_url, job_folder, catalog_builder, tmp_path):
    class Accounts:
        def login(self, page, digest, host):
            page.fill("#login_email", "jane.doe@example.com")
            page.fill("#login_password", "not-recorded")
            page.click("#btn-signin")
            page.wait_for_selector("#first_name")
            return True

        def signup(self, page, digest, host):
            return False

    _enqueue(job_folder, fixture_url("login_wall.html"))
    runner = _runner(context, tmp_path)
    runner.accounts = Accounts()
    out = runner.drain(cap=1)[0]
    assert out.status == "submitted", out
    record = Path(out.record_path).read_text(encoding="utf-8")
    assert "not-recorded" not in record
    assert "State: login_wall" in record and "State: confirmation" in record


# --- the code gate: the inbox hook, or the park note ------------------------------------

def test_code_gate_parks_with_the_code_note_by_default(
        context, fixture_url, job_folder, catalog_builder, tmp_path):
    _enqueue(job_folder, fixture_url("code_gate.html"))
    runner = _runner(context, tmp_path)
    runner.inbox = apply_run.NotConfigured()
    out = runner.drain(cap=1)[0]
    assert out.status == "needs_human", out
    assert out.reason == "emailed code needed"
    assert _entry()["tab_note"] == apply_run.CODE_NOTE ==         "enter the emailed code manually, then Re-queue"


def test_code_gate_with_an_inbox_hook_fills_the_code_and_reaches_confirmation(
        context, fixture_url, job_folder, catalog_builder, tmp_path):
    class Inbox:
        calls = []

        def fetch_code(self, page, site, inbox_url):
            self.calls.append((site, inbox_url))
            return "MKPZ3QRA"

    _enqueue(job_folder, fixture_url("code_gate.html"))
    runner = _runner(context, tmp_path)
    runner.inbox = Inbox()
    out = runner.drain(cap=1)[0]
    assert out.status == "submitted", out
    assert Inbox.calls == [("127.0.0.1", "https://mail.example.com/inbox")]
    record = Path(out.record_path).read_text(encoding="utf-8")
    assert "State: code_gate" in record and "Security code: <hidden>" in record
    assert "MKPZ3QRA" not in record                    # the emailed code is never written
    assert "Verify and continue (advance)" in record


@pytest.mark.parametrize("fixture,existing", [("login_wall.html", True), ("signup.html", False),
                                             ("login_wall.html", False)])
def test_default_accounts_continue_and_keep_password_private(
        context, fixture_url, job_folder, catalog_builder, tmp_path, monkeypatch, caplog,
        fixture, existing):
    secret = "synthetic-SP5-password"
    monkeypatch.setattr(ats_accounts, "_get_master_password", lambda: secret)
    if existing:
        ats_accounts.record("127.0.0.1", "existing@example.com")
    _enqueue(job_folder, fixture_url(fixture))
    with caplog.at_level("INFO"):
        out = _runner(context, tmp_path, auto_apply_submit=False).drain(cap=1)[0]
    assert out.status == "ready_to_submit", out
    record = Path(out.record_path).read_text(encoding="utf-8")
    assert secret not in record + caplog.text
    assert "password filled" in caplog.text
    account = ats_accounts.lookup("127.0.0.1")
    assert account["email"] == ("existing@example.com" if existing else "jane.doe@example.com")
    # the account step is in the record: the address, the hidden password, the click
    assert f"Email: {account['email']}" in record
    assert "Password: <hidden>" in record
    assert ("Sign in (advance)" in record) or ("Create account (advance)" in record)


def test_a_two_step_sign_in_types_the_address_then_the_password(
        context, fixture_url, job_folder, catalog_builder, tmp_path, monkeypatch, caplog):
    # the iCIMS shape probed 2026-09-22: the address and Next on one screen,
    # the password on the next; no ledger entry, the address is the signup email
    secret = "synthetic-two-step-password"
    monkeypatch.setattr(ats_accounts, "_get_master_password", lambda: secret)
    _enqueue(job_folder, fixture_url("login_email_first.html"))
    with caplog.at_level("INFO"):
        out = _runner(context, tmp_path, auto_apply_submit=False).drain(cap=1)[0]
    assert out.status == "ready_to_submit", out
    record = Path(out.record_path).read_text(encoding="utf-8")
    assert secret not in record + caplog.text
    assert "Email: jane.doe@example.com" in record and "Next (advance)" in record
    assert "Password: <hidden>" in record and "Sign in (advance)" in record
    assert record.count("State: login_wall") == 2


def test_an_address_screen_with_another_box_goes_as_the_form_and_the_password_follows(
        context, fixture_url, job_folder, catalog_builder, tmp_path, monkeypatch):
    # the third review: a country picker beside the address makes the screen
    # the form (the account step takes account boxes alone); the password
    # screen after it still signs in
    monkeypatch.setattr(ats_accounts, "_get_master_password", lambda: "synthetic-password")
    body = Path(__file__).parent.joinpath("fixtures", "forms", "login_email_first.html").read_text(
        encoding="utf-8").replace(
        '<p><button type="button" id="btn-next">',
        '<label for="country">Country</label><select id="country"><option value="">Select'
        '</option><option>United States</option><option>Canada</option></select>'
        '<p><button type="button" id="btn-next">')
    url = fixture_url("login_email_first_country.html")
    context.route(url, lambda route: route.fulfill(body=body, content_type="text/html"))
    _enqueue(job_folder, url)
    out = _runner(context, tmp_path, auto_apply_submit=False).drain(cap=1)[0]
    assert out.status == "ready_to_submit", out
    record = Path(out.record_path).read_text(encoding="utf-8")
    assert "Password: <hidden>" in record and "Sign in (advance)" in record


def test_a_password_screen_without_the_address_step_or_an_account_parks(
        context, fixture_url, job_folder, catalog_builder, tmp_path, monkeypatch):
    monkeypatch.setattr(ats_accounts, "_get_master_password", lambda: "synthetic-password")
    _enqueue(job_folder, fixture_url("login_password_step.html"))
    out = _runner(context, tmp_path, auto_apply_submit=False).drain(cap=1)[0]
    assert out.status == "needs_human" and out.reason.startswith("login wall ("), out


def test_a_signup_asking_for_an_ssn_parks_for_the_human_without_asking_for_it(
        context, fixture_url, job_folder, catalog_builder, tmp_path, monkeypatch):
    monkeypatch.setattr(ats_accounts, "_get_master_password", lambda: "synthetic-password")
    _enqueue(job_folder, fixture_url("signup_unanswerable.html"))
    out = _runner(context, tmp_path).drain(cap=1)[0]
    assert out.status == "needs_human", out
    assert out.reason == apply_judge.sensitive_reason("Social Security Number")
    assert _entry()["missing_answers"] == []
    page = next(p for p in context.pages if not p.is_closed())
    assert page.evaluate("window.__created") == 0
    assert ats_accounts.lookup("127.0.0.1") is None


def test_the_password_is_never_typed_on_linkedin_whatever_led_there(
        context, fixture_url, job_folder, catalog_builder, tmp_path, monkeypatch):
    # the SP8-live review: a sign-up link on a login screen could lead to
    # linkedin.com/signup and the old path typed the master password there;
    # the page stands in for LinkedIn through a route, no network is used
    html = (fixture_url("signup.html"))
    body = Path(__file__).parent.joinpath("fixtures", "forms", "signup.html").read_text(
        encoding="utf-8")
    context.route("https://www.linkedin.com/**",
                  lambda route: route.fulfill(body=body, content_type="text/html"))
    monkeypatch.setattr(ats_accounts, "_get_master_password", lambda: "synthetic-password")
    typed = []
    monkeypatch.setattr(ats_accounts, "fill_password", lambda *a: typed.append(a) or True)
    _enqueue(job_folder, html)
    run = apply_run._JobRun(_runner(context, tmp_path), context, _entry())
    run._prepare()
    run.ats_hosts.add("careers.fabrikam.example")
    run.page = context.new_page()
    run.page.goto("https://www.linkedin.com/signup")
    digest = apply_form.extract(run.page)
    run._new_page_record("signup_form", 1.0)
    with pytest.raises(apply_run._Parked, match="outside the application site"):
        run.accounts._fill(run.page, digest, "careers.fabrikam.example",
                           "jane.doe@example.com", True)
    assert typed == []
    assert ats_accounts.lookup("careers.fabrikam.example") is None


def test_default_signup_fills_the_candidates_name_fields_from_the_catalog(
        context, fixture_url, job_folder, catalog_builder, tmp_path, monkeypatch, caplog):
    """A signup form that also asks for a name is filled from the fact catalog,
    which is the only place a name may come from."""
    secret = "synthetic-SP5-password"
    monkeypatch.setattr(ats_accounts, "_get_master_password", lambda: secret)
    _enqueue(job_folder, fixture_url("signup_with_names.html"))
    with caplog.at_level("INFO"):
        out = _runner(context, tmp_path, auto_apply_submit=False).drain(cap=1)[0]
    assert out.status == "ready_to_submit", out
    record = Path(out.record_path).read_text(encoding="utf-8")
    assert secret not in record + caplog.text
    assert ats_accounts.lookup("127.0.0.1")["email"] == "jane.doe@example.com"


def test_two_signup_links_to_one_target_still_reach_the_signup_form(
        context, fixture_url, job_folder, catalog_builder, tmp_path, monkeypatch):
    """A header link and a body link pointing at the same page are one offer."""
    monkeypatch.setattr(ats_accounts, "_get_master_password", lambda: "synthetic-password")
    _enqueue(job_folder, fixture_url("login_two_signup_links.html"))
    out = _runner(context, tmp_path, auto_apply_submit=False).drain(cap=1)[0]
    assert out.status == "ready_to_submit", out
    assert ats_accounts.lookup("127.0.0.1")["email"] == "jane.doe@example.com"


def test_an_absolute_and_a_relative_signup_link_to_one_target_are_one_offer(
        context, fixture_url, job_folder, catalog_builder, tmp_path, monkeypatch):
    """The header link spells the page out in full and the body link is
    relative; resolved against the page they are the same target."""
    monkeypatch.setattr(ats_accounts, "_get_master_password", lambda: "synthetic-password")
    _enqueue(job_folder, fixture_url("login_signup_links_mixed.html"))
    out = _runner(context, tmp_path, auto_apply_submit=False).drain(cap=1)[0]
    assert out.status == "ready_to_submit", out
    assert ats_accounts.lookup("127.0.0.1")["email"] == "jane.doe@example.com"


def test_a_signup_link_that_leaves_the_allowed_sites_parks_with_that_reason(
        context, fixture_url, job_folder, catalog_builder, tmp_path, monkeypatch):
    """The host check inside the login hook is the loop's own park; the hook
    passes it on, so the reason names the host it refused to visit."""
    monkeypatch.setattr(ats_accounts, "_get_master_password", lambda: "synthetic-password")
    _enqueue(job_folder, fixture_url("login_signup_off_host.html"))
    out = _runner(context, tmp_path, auto_apply_submit=False).drain(cap=1)[0]
    assert out.status == "needs_human", out
    assert out.reason == "left the allowed sites: localhost"
    assert ats_accounts.lookup("127.0.0.1") is None


def test_off_host_subresources_on_the_sign_in_click_do_not_park_the_job(
        context, fixture_url, job_folder, catalog_builder, tmp_path, monkeypatch):
    """A bot-check script and a webfont from a CDN carry no credentials; they
    are aborted quietly and the login goes through."""
    monkeypatch.setattr(ats_accounts, "_get_master_password", lambda: "synthetic-password")
    ats_accounts.record("127.0.0.1", "synthetic@example.com")
    _enqueue(job_folder, fixture_url("login_subresource.html"))
    out = _runner(context, tmp_path, auto_apply_submit=False).drain(cap=1)[0]
    assert out.status == "ready_to_submit", out
    record = Path(out.record_path).read_text(encoding="utf-8")
    assert "left the allowed sites" not in record
    assert "State: login_wall" in record


def test_a_signup_that_parks_off_host_still_records_the_account(
        context, fixture_url, job_folder, catalog_builder, tmp_path, monkeypatch):
    """The create-account click landed, so the account may already exist; the
    ledger has to say so or the next run signs up again with the same email."""
    monkeypatch.setattr(ats_accounts, "_get_master_password", lambda: "synthetic-password")
    _enqueue(job_folder, fixture_url("signup_cross_host.html"))
    out = _runner(context, tmp_path).drain(cap=1)[0]
    assert out.status == "needs_human", out
    assert "left the allowed sites: localhost" in out.reason
    assert ats_accounts.lookup("127.0.0.1")["email"] == "jane.doe@example.com"
    # the aborted navigation leaves the tab on a browser error page; the note
    # names the page the human has to come back to
    tab_note = _entry()["tab_note"]
    assert tab_note.startswith(fixture_url("signup_cross_host.html")), tab_note
    assert "chrome-error" not in tab_note
    assert "Create an account - Synthetic Careers" in tab_note


def test_default_accounts_block_cross_host_credential_request_before_it_reaches_server(
        context, fixture_url, job_folder, catalog_builder, tmp_path, monkeypatch):
    import conftest_browser

    received = []
    original = conftest_browser._QuietHandler.do_GET

    def _record(self):
        if self.path.startswith("/forms/credential_sink.html"):
            received.append(self.path)
        return original(self)

    monkeypatch.setattr(conftest_browser._QuietHandler, "do_GET", _record)
    monkeypatch.setattr(ats_accounts, "_get_master_password", lambda: "synthetic-password")
    ats_accounts.record("127.0.0.1", "synthetic@example.com")
    _enqueue(job_folder, fixture_url("login_cross_host.html"))

    out = _runner(context, tmp_path).drain(cap=1)[0]

    assert out.status == "needs_human", out
    assert received == []
    page = next(p for p in context.pages if not p.is_closed())
    assert "Credential request reached" not in page.content()


def test_default_inbox_after_submit_reaches_confirmation(
        context, fixture_url, fixtures_server, job_folder, catalog_builder, tmp_path, caplog):
    """The fixture inbox carries Greenhouse's code mail among decoys (an Ashby
    code, an order number); the job's ATS is Greenhouse, which is what the
    from-site question asks about, since the form's host names no sender."""
    _enqueue(job_folder, fixture_url("submit_code.html"))
    apply_queue.update("42", ats={"system": "greenhouse"})
    runner = _runner(context, tmp_path)
    runner._run_context = {**_RUN_CONTEXT, "inbox_url": fixtures_server + "/inbox/outlook_list.html"}
    with caplog.at_level("INFO"):
        out = runner.drain(cap=1)[0]
    assert out.status == "submitted", out
    assert out.reason == "confirmation page after the emailed code"
    record = Path(out.record_path).read_text(encoding="utf-8")
    assert "Security code: <hidden>" in record
    assert "MKPZ3QRA" not in record + caplog.text


def test_default_inbox_hook_names_the_entrys_ats_and_company(
        context, fixture_url, job_folder, catalog_builder, tmp_path, monkeypatch):
    seen = {}

    def _fetch(page, site, inbox_url, **kw):
        seen.update(site=site, inbox_url=inbox_url, **kw)
        return None
    monkeypatch.setattr(apply_run.apply_inbox, "fetch_code", _fetch)
    _enqueue(job_folder, fixture_url("code_gate.html"))
    apply_queue.update("42", ats={"system": "greenhouse"})
    out = _runner(context, tmp_path).drain(cap=1)[0]
    assert out.status == "needs_human" and out.reason == "emailed code needed"
    assert seen["site"] == "127.0.0.1"
    assert seen["inbox_url"] == "https://mail.example.com/inbox"
    assert seen["ats"] == "greenhouse" and seen["company"] == "Fabrikam"


def test_code_fill_error_never_logs_or_records_code(
        context, fixture_url, job_folder, catalog_builder, tmp_path, monkeypatch, caplog):
    from playwright.sync_api import Locator

    class Inbox:
        def fetch_code(self, page, site, inbox_url):
            return "MKPZ3QRA"

    def fail(self, value, **kwargs):
        raise RuntimeError("fill failed: " + value)

    monkeypatch.setattr(Locator, "fill", fail)
    _enqueue(job_folder, fixture_url("code_gate.html"))
    runner = _runner(context, tmp_path)
    runner.inbox = Inbox()
    out = runner.drain(cap=1)[0]
    assert out.status == "needs_human"
    assert "MKPZ3QRA" not in Path(out.record_path).read_text(encoding="utf-8") + caplog.text


# --- the entry's artifacts name the PDFs -----------------------------------------------

def test_entry_artifacts_override_the_folder_pdfs_and_a_missing_one_is_recorded(
        context, job_folder, tmp_path, catalog_builder):
    other = tmp_path / "elsewhere" / "Tailored_Resume.pdf"
    other.parent.mkdir()
    other.write_bytes(_PDF)
    e = _enqueue(job_folder, "https://boards.greenhouse.io/acme/jobs/1")
    apply_queue.set_artifacts("42", {"resume_pdf": str(other),
                                     "cover_letter_pdf": str(tmp_path / "gone.pdf")})
    e = _entry()
    run = apply_run._JobRun(_runner(context, tmp_path), context, e)
    run._prepare()
    assert run.catalog.value("resume_file") == str(other)
    assert run.catalog.value("cover_letter_file") == ""
    assert run.catalog.has("cover_letter_file") is False
    missing = _entry()["missing_answers"]
    assert [m["question"] for m in missing] == ["Cover letter PDF"]
    assert missing[0]["context"] == f"the file is missing: {tmp_path / 'gone.pdf'}"


def test_entry_without_artifact_paths_keeps_the_folder_scan(
        context, job_folder, tmp_path, catalog_builder):
    e = apply_queue.new_entry("42", apply_url="https://boards.greenhouse.io/acme/jobs/1")
    e["artifacts"]["apply_md"] = str(job_folder / "apply.md")
    apply_queue.enqueue(e)
    run = apply_run._JobRun(_runner(context, tmp_path), context, _entry())
    run._prepare()
    assert run.catalog.value("resume_file") == str(job_folder / "Jane_Doe_Resume.pdf")
    assert _entry()["missing_answers"] == []


# --- the job posting: Apply opens a new tab that the loop follows -----------------------

def test_job_posting_apply_opens_a_popup_that_the_loop_follows(
        context, fixture_url, job_folder, catalog_builder, tmp_path):
    _enqueue(job_folder, fixture_url("job_posting.html"))
    out = _runner(context, tmp_path).drain(cap=1)[0]
    assert out.status == "submitted", out
    record = Path(out.record_path).read_text(encoding="utf-8")
    assert "State: job_posting" in record
    assert "Apply now (apply_entry)" in record
    assert "ashby_steps.html" in record


def test_linkedin_shaped_posting_follows_its_apply_link_through_the_redirect(
        context, fixture_url, job_folder, catalog_builder, tmp_path, monkeypatch):
    # the page the 2026-09-22 live run parked on: the Apply entry is a plain
    # link, the header search and the footer language picker are site chrome,
    # and the new tab is a redirector whose script moves on to the ATS. Served
    # off LinkedIn, the redirector moves on by its 1.5 s timer alone: the
    # settle keeps a quiet window longer than that
    monkeypatch.setattr(apply_run.apply_fill, "SETTLE_QUIET_S", 2.0)
    _enqueue(job_folder, fixture_url("linkedin_posting.html"))
    out = _runner(context, tmp_path).drain(cap=1)[0]
    assert out.status == "submitted", out
    record = Path(out.record_path).read_text(encoding="utf-8")
    assert "State: job_posting" in record
    assert "Apply (apply_entry)" in record
    assert "ashby_steps.html" in record
    assert "Select language" not in record


class _PageStateJudge(jev.FakeJev):
    """The fake, with the page state of every page on `HOST` read as `STATE`
    at `CONF`, its Nouls as `NOULS` says (`apply_harness.read_as`): a live
    misread, scripted."""
    HOST = ""
    STATE = ""
    CONF = 0.0
    NOULS = "coherent"

    def judge(self, state, questions):
        out = super().judge(state, questions)
        if "page_state" in out and state["page"]["url_host"] == self.HOST:
            h.read_as(out, self.STATE, self.CONF, nouls=self.NOULS)
        return out


def _page_state_judge(host, state, conf, nouls="coherent"):
    return type("Judge", (_PageStateJudge,), {"HOST": host, "STATE": state, "CONF": conf,
                                              "NOULS": nouls})()


_LINKEDIN_JOB = "https://www.linkedin.com/jobs/view/4438751519/"


def _serve_linkedin_posting(context, fixture_url):
    """The LinkedIn-shaped posting at a LinkedIn job URL (a route; no network),
    its Apply link going through LinkedIn's `/safety/go/` hop (the fixture
    redirector, routed) to the fixture form, as the live page's does."""
    forms = Path(__file__).parent.joinpath("fixtures", "forms")
    target = fixture_url("ashby_steps.html")
    body = (forms / "linkedin_posting.html").read_text(encoding="utf-8").replace(
        'href="linkedin_redirect.html"',
        f'href="https://www.linkedin.com/safety/go/?url={target}"')
    hop = (forms / "linkedin_redirect.html").read_text(encoding="utf-8").replace(
        "location.replace('ashby_steps.html'); }, 1500)", f"location.replace('{target}'); }}, 400)")
    context.route("https://www.linkedin.com/**",
                  lambda route: route.fulfill(body=body, content_type="text/html"))
    context.route("https://www.linkedin.com/safety/go/**",
                  lambda route: route.fulfill(body=hop, content_type="text/html"))


@pytest.mark.parametrize("state, conf", [("application_form", 0.33), ("review_page", 0.90),
                                         ("other", 0.20), ("job_posting", 0.10)])
def test_a_linkedin_posting_is_the_posting_whatever_the_judge_reads(
        context, fixture_url, job_folder, catalog_builder, tmp_path, state, conf):
    # the 2026-09-22 run parked GTS on "unsure what this page is (application_form,
    # 0.33)": LinkedIn's header and upsells filled the judge's view of the page
    _serve_linkedin_posting(context, fixture_url)
    _enqueue(job_folder, _LINKEDIN_JOB)
    runner = _runner(context, tmp_path)
    runner.jev = _page_state_judge("www.linkedin.com", state, conf)
    out = runner.drain(cap=1)[0]
    assert out.status == "submitted", out
    record = Path(out.record_path).read_text(encoding="utf-8")
    assert "Apply (apply_entry)" in record
    assert "ashby_steps.html" in record


@pytest.mark.parametrize("state", ["login_wall", "error_or_dead"])
def test_a_linkedin_posting_the_judge_reads_as_signed_out_or_closed_still_goes_on(
        context, fixture_url, job_folder, catalog_builder, tmp_path, state):
    # SP2: a LinkedIn job page is read without the judge; its own marks
    # (signed out, closed, applied) park it (tests/test_apply_entry.py), and
    # a confident misread no longer does
    _serve_linkedin_posting(context, fixture_url)
    _enqueue(job_folder, _LINKEDIN_JOB)
    runner = _runner(context, tmp_path)
    runner.jev = _page_state_judge("www.linkedin.com", state, 0.90)
    out = runner.drain(cap=1)[0]
    assert out.status == "submitted", out
    assert "ashby_steps.html" in Path(out.record_path).read_text(encoding="utf-8")


class _UnsureJudge(jev.FakeJev):
    """The fake, with every application form read at 0.33, its details Noul
    leaning against it (0.20) and every other page-read Noul saying nothing:
    the combined read stays under the floor, so the unsure rule decides."""

    def judge(self, state, questions):
        out = super().judge(state, questions)
        a = out.get("page_state")
        if a is not None and a.choice == "application_form":
            h.read_as(out, a.choice, 0.33, nouls="neutral")
            if "page_applicant_details" in out:
                out["page_applicant_details"] = jev.Answer(kind="noul", noul=0.20)
        return out


def test_an_unsure_read_of_a_form_is_acted_on_through_the_forms_own_gates(
        context, fixture_url, job_folder, catalog_builder, tmp_path):
    # an unsure read of a form (under the floor twice) goes on as its guess,
    # and the form's own gates decide the rest (review I5: the path is the
    # unsure rule's, `_check_unsure`, never the sure read's)
    _enqueue(job_folder, fixture_url("ashby_steps.html"))
    runner = _runner(context, tmp_path)
    runner.jev = _UnsureJudge()
    out = runner.drain(cap=1)[0]
    assert out.status == "submitted", out
    trace = sorted((job_folder / "apply_trace").glob("attempt-*"))[-1]
    first = json.loads((trace / "page-1.json").read_text(encoding="utf-8"))
    assert first["answers"]["page_state_judged"]["confidence"] == 0.33
    assert first["state"] == "application_form"
    assert first["confidence"] < apply_judge.PAGE_STATE_MIN_CONF, first["confidence"]
    decided = [e["what"] for e in first["events"] if e["kind"] == "decision"]
    assert "reread_unsure" in decided and "unsure_goes_on" in decided, decided


# A page whose structure places it nowhere (no box, no Apply, no Next): the
# judge's read alone decides it
_NOWHERE = ("<!doctype html><html><head><title>Life at Fabrikam</title></head><body>"
            "<h1>Life at Fabrikam</h1><p>" + "Our teams build analytics for retail partners "
            "across three continents, and we care about growth and learning. " * 3
            + "</p><button type='button'>Menu</button></body></html>")


@pytest.mark.parametrize("state", ["other", "captcha_or_bot_check", "confirmation"])
def test_an_unsure_read_the_run_cannot_act_on_parks(
        context, job_folder, catalog_builder, tmp_path, state):
    context.route("https://careers.fabrikam.example/**",
                  lambda route: route.fulfill(body=_NOWHERE, content_type="text/html"))
    url = "https://careers.fabrikam.example/life"
    _enqueue(job_folder, url)
    runner = _runner(context, tmp_path, auto_apply_headless=False)
    runner.jev = _page_state_judge(apply_run._host(url), state, 0.30, nouls="neutral")
    out = runner.drain(cap=1)[0]
    assert out.status == "needs_human", out
    assert out.reason.startswith(f"unsure what this page is ({state}, 0.30); reads: "), out


class _FormAsAccountJudge(jev.FakeJev):
    """The fake, with every page that carries fields read as `STATE` at `CONF`."""
    STATE = ""
    CONF = 0.0

    def judge(self, state, questions):
        out = super().judge(state, questions)
        if "page_state" in out and state.get("fields"):
            h.read_as(out, self.STATE, self.CONF)
        return out


@pytest.mark.parametrize("submit_on", [False, True])
@pytest.mark.parametrize("state, conf", [("login_wall", 0.30), ("signup_form", 0.30),
                                         ("login_wall", 0.90), ("signup_form", 0.90)])
def test_an_application_form_read_as_a_sign_in_is_never_sent_by_the_account_step(
        context, fixture_url, job_folder, catalog_builder, tmp_path, monkeypatch,
        state, conf, submit_on):
    # the review of the unsure-read change: the account step filled a one-page
    # form it took for a sign-in and clicked its "Submit application" in park
    # mode. A page of form boxes is the form, whatever it is read as: its own
    # gate parks it or sends it.
    monkeypatch.setattr(ats_accounts, "_get_master_password", lambda: "synthetic-password")
    _enqueue(job_folder, fixture_url("lever_single.html"))
    runner = _runner(context, tmp_path, auto_apply_submit=submit_on)
    runner.jev = type("Judge", (_FormAsAccountJudge,), {"STATE": state, "CONF": conf})()
    out = runner.drain(cap=1)[0]
    record = Path(out.record_path).read_text(encoding="utf-8")
    assert "(advance)" not in record
    if submit_on:
        assert out.status == "submitted", out
        assert "SUBMIT CLICKED" in record
        return
    page = next(p for p in context.pages if not p.is_closed())
    assert page.locator("body[data-submitted]").count() == 0
    assert (out.status, out.reason) == ("ready_to_submit", "auto_apply_submit is off"), out


_COMBINED = """<!doctype html><html><head><title>Apply</title></head><body>
<h1>Create your candidate account and apply</h1>
<label>First name * <input name="first" required></label>
<label>Last name * <input name="last" required></label>
<label>Email * <input type="email" name="email" required></label>
<label>Phone <input type="tel" name="phone"></label>
__RESUME__
__EXTRA__
<label>Password * <input type="password" name="pw" autocomplete="new-password" required></label>
<button type="button" onclick="document.body.dataset.submitted = 1; document.body.innerHTML =
  '<h1>Application received</h1><p>Thank you for applying.</p>'">__BUTTON__</button>
</body></html>"""


_COMBINED_URL = "https://careers.fabrikam.example/apply/42"


def _serve_combined(context, button, resume=False, html=_COMBINED, extra=False):
    """`html` at the application's URL. `resume` adds a required resume box
    (the page is then the form); `extra` a required question only an
    application asks (a LinkedIn profile)."""
    body = html.replace("__BUTTON__", button).replace(
        "__RESUME__", '<label>Resume * <input type="file" name="resume" required></label>'
        if resume else "").replace(
        "__EXTRA__", '<label>LinkedIn profile * <input type="url" name="linkedin" required>'
                     '</label>' if extra else "")
    context.route("https://careers.fabrikam.example/**",
                  lambda route: route.fulfill(body=body, content_type="text/html"))


def _count_password_fills(monkeypatch):
    """Typed master passwords, counted; the real filler still types them."""
    monkeypatch.setattr(ats_accounts, "_get_master_password", lambda: "synthetic-password")
    typed = []
    real = ats_accounts.fill_password
    monkeypatch.setattr(ats_accounts, "fill_password",
                        lambda *a: typed.append(1) or real(*a))
    return typed


@pytest.mark.parametrize("submit_on", [False, True])
@pytest.mark.parametrize("button, resume, conf", [
    # a resume box makes the page the form
    ("Create account and apply", True, 0.90),
    ("Register and submit application", True, 0.90),
    ("Create account and apply", True, 0.30),
    # the account boxes and a question only an application asks: the account
    # step hands the page to the form step, since its button sends it
    ("Create account and apply", False, 0.90),
    ("Sign in and apply", False, 0.90),
    ("Sign in and apply", False, 0.30),
    ("Complete application", False, 0.90)])
def test_a_sign_up_inside_the_application_form_takes_the_password_and_stops_at_the_gate(
        context, job_folder, catalog_builder, tmp_path, monkeypatch, submit_on, button,
        resume, conf):
    # the second review: one page that creates the account and sends the
    # application. The master password is for job applications only (the
    # user's rule, 2026-09-22): the form step types it, and only the submit
    # gate sends, so park mode stops there with the page filled
    typed = _count_password_fills(monkeypatch)
    _serve_combined(context, button, resume, extra=not resume)
    _enqueue(job_folder, _COMBINED_URL)
    runner = _runner(context, tmp_path, auto_apply_submit=submit_on)
    runner.jev = type("Judge", (_FormAsAccountJudge,), {"STATE": "signup_form", "CONF": conf})()
    out = runner.drain(cap=1)[0]
    record = Path(out.record_path).read_text(encoding="utf-8")
    assert typed == [1], out
    assert re.search(r"- Password[^:\n]*: <hidden>", record), record
    assert "synthetic-password" not in record
    assert "(advance)" not in record
    assert all(m["question"] != "Password" for m in _entry()["missing_answers"])
    assert ats_accounts.lookup(_COMBINED_URL)["method"] == "master_password"
    if submit_on:
        assert out.status == "submitted", out
        assert "SUBMIT CLICKED" in record
        return
    page = next(p for p in context.pages if not p.is_closed())
    assert page.locator("body[data-submitted]").count() == 0, out
    assert page.locator("[name=pw]").input_value() == "synthetic-password"
    assert (out.status, out.reason) == ("ready_to_submit", "auto_apply_submit is off"), out


def test_a_plain_account_button_on_the_application_form_is_an_advance(
        context, job_folder, catalog_builder, tmp_path, monkeypatch):
    # "Create account" with the resume on the page makes the account and goes
    # on to the application's next step: the form step clicks it
    typed = _count_password_fills(monkeypatch)
    _serve_combined(context, "Create account", resume=True)
    _enqueue(job_folder, _COMBINED_URL)
    runner = _runner(context, tmp_path, auto_apply_submit=False)
    runner.jev = type("Judge", (_FormAsAccountJudge,), {"STATE": "signup_form", "CONF": 0.9})()
    out = runner.drain(cap=1)[0]
    assert typed == [1], out
    assert "Create account (advance)" in Path(out.record_path).read_text(encoding="utf-8")


_ACCOUNT_ONLY = """<!doctype html><html><head><title>Sign up</title></head><body>
<label>Email * <input type="email" name="email" required></label>
<label>Password * <input type="password" name="pw" autocomplete="new-password" required></label>
<button type="button" onclick="document.body.dataset.submitted = 1">__BUTTON__</button>
</body></html>"""


_SIGN_IN_TERMS = """<!doctype html><html><head><title>Sign in</title></head><body>
<label>Email * <input type="email" name="email" required></label>
<label>Password * <input type="password" name="pw" autocomplete="current-password" required>
</label>
<label><input type="checkbox" name="terms" required> I agree to the terms of use *</label>
<button type="button" onclick="document.body.dataset.submitted = 1">__BUTTON__</button>
</body></html>"""


@pytest.mark.parametrize("submit_on", [False, True])
@pytest.mark.parametrize("button, html", [
    ("Create account and apply", _ACCOUNT_ONLY), ("Submit", _ACCOUNT_ONLY),
    # a sign-up's own boxes (the name, the phone) and the terms are no application
    ("Create account and apply", _COMBINED), ("Sign in and apply", _COMBINED),
    ("Sign in and apply", _SIGN_IN_TERMS)])
def test_an_account_screen_alone_whose_button_reads_as_sending_parks_before_typing(
        context, job_folder, catalog_builder, tmp_path, monkeypatch, submit_on, button, html):
    # with a sign-up's boxes alone, the gate cannot tell a button that starts
    # the application from one that sends it: submitting on, a wrong
    # "submitted" would lose the job (the fourth review), so the human signs in
    typed = _count_password_fills(monkeypatch)
    _serve_combined(context, button, html=html)
    _enqueue(job_folder, _COMBINED_URL)
    runner = _runner(context, tmp_path, auto_apply_submit=submit_on)
    runner.jev = type("Judge", (_FormAsAccountJudge,), {"STATE": "signup_form", "CONF": 0.9})()
    out = runner.drain(cap=1)[0]
    page = next(p for p in context.pages if not p.is_closed())
    assert page.locator("body[data-submitted]").count() == 0, out
    assert out.status == "needs_human" and "reads as sending the application" in out.reason, out
    assert typed == []


_REJECTING_FORM = """<!doctype html><html><head><title>Apply</title></head><body>
<h1>Apply</h1>
<label>Resume * <input type="file" name="resume" required></label>
<label>Email * <input type="email" name="email" required></label>
<label>Password * <input type="password" name="pw" autocomplete="current-password" required>
</label>
<button type="button" onclick="document.querySelector('h1').textContent =
  'That password is not right. Try again.'; document.querySelector('[name=pw]').value = ''">
Continue</button></body></html>"""


def test_a_form_gets_the_master_password_once_per_site(
        context, job_folder, catalog_builder, tmp_path, monkeypatch):
    # a rejected password comes back as the same form: typing it again only
    # moves the account toward a lockout
    typed = _count_password_fills(monkeypatch)
    _serve_combined(context, "", html=_REJECTING_FORM)
    _enqueue(job_folder, _COMBINED_URL)
    runner = _runner(context, tmp_path, auto_apply_submit=False)
    runner.jev = type("Judge", (_FormAsAccountJudge,),
                      {"STATE": "application_form", "CONF": 0.9})()
    out = runner.drain(cap=1)[0]
    assert typed == [1], out
    # SP7 (ACC-12): the park names what the page says about it; a sign-in's
    # box is never typed again, whatever the page says
    assert (out.status, out.reason) == (
        "needs_human", "the form on careers.fabrikam.example asked for the master password "
                       "again (the page says 'That password is not right. Try again.')"), out


_SIGN_UP_THEN_APPLY = """<!doctype html><html><head><title>Create account</title></head><body>
<label>First name * <input name="first" required></label>
<label>Last name * <input name="last" required></label>
<label>Email * <input type="email" name="email" required></label>
<label>Password * <input type="password" name="pw" autocomplete="new-password" required></label>
<label>Confirm password * <input type="password" name="pw2" autocomplete="new-password"
  required></label>
<button type="button" onclick="document.body.innerHTML =
  '<h1>Your account is ready</h1><a href=&quot;/apply/42/form&quot;>Apply now</a>'">
Create account</button></body></html>"""
_THE_FORM = """<!doctype html><html><head><title>Apply</title></head><body>
<label>First name * <input name="first" required></label>
<button type="button" onclick="document.body.dataset.submitted = 1; document.body.innerHTML =
  '<h1>Application received</h1><p>Thank you for applying.</p>'">Submit application</button>
</body></html>"""


@pytest.mark.parametrize("submit_on", [False, True])
def test_a_sign_up_read_as_the_form_does_not_count_as_the_filled_application(
        context, job_folder, catalog_builder, tmp_path, monkeypatch, submit_on):
    # the fourth review: the form step made the account and marked the run's
    # form filled, so the "Apply now" after it went to the submit gate and a
    # submit-mode run finished "submitted" with nothing sent. A page of a
    # sign-up's boxes and a password makes an account, not the application
    typed = _count_password_fills(monkeypatch)
    context.route("https://careers.fabrikam.example/**", lambda route: route.fulfill(
        body=_THE_FORM if route.request.url.endswith("/form") else _SIGN_UP_THEN_APPLY,
        content_type="text/html"))
    _enqueue(job_folder, _COMBINED_URL)

    class Judge(_FormAsAccountJudge):
        """Every page with fields a form, the page after the sign-up a posting."""
        STATE, CONF = "application_form", 0.9

        def judge(self, state, questions):
            out = super().judge(state, questions)
            if "page_state" in out and "account is ready" in state["page"]["headline_text"]:
                out["page_state"] = jev.Answer(kind="choice", choice="job_posting",
                                               probabilities={"job_posting": 0.9},
                                               confidence=0.9)
            return out

    runner = _runner(context, tmp_path, auto_apply_submit=submit_on)
    runner.jev = Judge()
    out = runner.drain(cap=1)[0]
    record = Path(out.record_path).read_text(encoding="utf-8")
    assert typed == [1, 1], out
    assert "Create account (advance)" in record
    if submit_on:
        assert "Apply now (apply_entry)" in record, record
        assert (out.status, out.reason) == ("submitted", "confirmation page"), out
        assert record.count("SUBMIT CLICKED") == 1
    else:
        # park mode takes no chance on an Apply after a page that took the
        # password: it may be an application's review (the next test). SP3's
        # container ruling: on a page read as a posting it is no submit
        # either, so the job waits for the person with the reason
        assert "Apply now (apply_entry)" not in record, record
        assert out.status == "needs_human", out
        assert out.reason.startswith("the Apply button (Apply now) may open or start"), out
    # the page made an account (a new password and its confirmation)
    assert ats_accounts.lookup(_COMBINED_URL)["email"] == "jane.doe@example.com"


_CONTACT_THEN_REVIEW = """<!doctype html><html><head><title>Apply</title></head><body>
<label>First name * <input name="first" required></label>
<label>Last name * <input name="last" required></label>
<label>Email * <input type="email" name="email" required></label>
<label>Password * <input type="password" name="pw" autocomplete="new-password" required></label>
<button type="button" onclick="review()">Next</button>
<script>
function review() {
  document.body.innerHTML = '<h1>Review your application</h1><button id="apply">Apply</button>';
  document.getElementById('apply').onclick = function () {
    document.body.dataset.submitted = '1';
    document.body.innerHTML = '<h1>Application received</h1>';
  };
}
</script></body></html>"""


@pytest.mark.parametrize("stored, html", [
    (True, _CONTACT_THEN_REVIEW),
    # round 4: an optional box left blank (no master password) counts too
    (False, _CONTACT_THEN_REVIEW.replace(
        'Password * <input type="password" name="pw" autocomplete="new-password" required>',
        'Password (optional) <input type="password" name="pw" autocomplete="new-password">'))])
def test_park_mode_never_clicks_an_apply_after_a_page_with_a_password_box(
        context, job_folder, catalog_builder, tmp_path, monkeypatch, stored, html):
    # the fourth review, round 3: an application that asks only for contact
    # details and a password, then a review page with "Apply" read as a
    # posting: park mode clicked it as the Apply entry and sent the application
    if stored:
        _count_password_fills(monkeypatch)
    _serve_combined(context, "", html=html)
    _enqueue(job_folder, _COMBINED_URL)

    class Judge(_FormAsAccountJudge):
        STATE, CONF = "application_form", 0.9

        def judge(self, state, questions):
            out = super().judge(state, questions)
            if "page_state" in out and "Review your" in state["page"]["headline_text"]:
                out["page_state"] = jev.Answer(kind="choice", choice="job_posting",
                                               probabilities={"job_posting": 0.9},
                                               confidence=0.9)
            return out

    runner = _runner(context, tmp_path, auto_apply_submit=False)
    runner.jev = Judge()
    out = runner.drain(cap=1)[0]
    page = next(p for p in context.pages if not p.is_closed())
    assert page.locator("body[data-submitted]").count() == 0, out
    # SP3's container ruling: an Apply on a fieldless page read as a posting
    # is no submit, even after a filled page; the job waits for the person
    assert out.status == "needs_human", out
    assert out.reason.startswith("the Apply button (Apply) may open or start an application"), out


@pytest.mark.parametrize("box, makes", [
    # a sign-in read as the form makes no account
    ('<label>Password <input type="password" name="pw" autocomplete="current-password" '
     'required></label>', False),
    ('<label>Password <input type="password" name="pw" required></label>', False),
    # a page that makes one: by its autocomplete, its label, or a confirmation
    ('<label>Password <input type="password" name="pw" autocomplete="new-password" required>'
     '</label>', True),
    ('<label>Create a password <input type="password" name="pw" required></label>', True),
    ('<label>Password <input type="password" name="pw" required></label><label>Confirm '
     'password <input type="password" name="pw2" required></label>', True)])
def test_the_ledger_takes_an_account_only_from_a_page_that_makes_one(
        context, job_folder, catalog_builder, tmp_path, monkeypatch, box, makes):
    typed = _count_password_fills(monkeypatch)
    run = _routed_unit_run(context, tmp_path, job_folder, f"""<body>
      <label>Email <input type="email" name="email" required></label>{box}</body>""")
    digest = apply_form.extract(run.page)
    plan = apply_judge.plan(digest, run.catalog, {})
    with run._password_guard() as guard:
        run._fill_passwords(digest, plan, run.pages[-1], guard)
    assert typed and all(typed)
    assert (ats_accounts.lookup(_COMBINED_URL) is not None) is makes


@pytest.mark.parametrize("read, conf", [("other", 0.3), ("job_posting", 0.9),
                                        ("review_page", 0.9)])
def test_after_a_handed_off_sign_ups_submit_only_a_confirmation_counts(
        context, job_folder, catalog_builder, tmp_path, monkeypatch, read, conf):
    # the fourth review, round 4: the gate clicked a handed-off sign-up's
    # "Create account and apply", a welcome page came next ("Start your
    # application"), and the job read "submitted (unconfirmed)"
    _count_password_fills(monkeypatch)
    welcome = ("<!doctype html><html><body><h1>Your account is ready</h1>"
               "<a href='/apply/42/start'>Start your application</a></body></html>")
    context.route("https://careers.fabrikam.example/**", lambda route: route.fulfill(
        body=welcome if route.request.url.endswith("/form") else _SIGN_UP_AND_APPLY,
        content_type="text/html"))
    _enqueue(job_folder, _COMBINED_URL)

    class Judge(_FormAsAccountJudge):
        STATE, CONF = "signup_form", 0.9

        def judge(self, state, questions):
            out = super().judge(state, questions)
            if "page_state" in out and "account is ready" in state["page"]["headline_text"]:
                out["page_state"] = jev.Answer(kind="choice", choice=read,
                                               probabilities={read: conf}, confidence=conf)
            return out

    runner = _runner(context, tmp_path, auto_apply_submit=True)
    runner.jev = Judge()
    out = runner.drain(cap=1)[0]
    assert out.status == "needs_human", out
    assert "the click may only have made the account" in out.reason, out


def test_an_optional_profile_password_on_a_sent_application_is_no_account_page(
        context, job_folder, catalog_builder, tmp_path, monkeypatch):
    # round 4: a stored master password went into an optional "save your
    # profile" box; the portal page after the send is no reason to stop
    typed = _count_password_fills(monkeypatch)
    portal = ("<!doctype html><html><body><h1>Your candidate profile</h1>"
              "<label>Headline <input name='headline'></label>"
              "<button>Save profile</button></body></html>")
    form = """<!doctype html><html><body>
      <label>First name * <input name="first" required></label>
      __RESUME__
      <label>Password (optional, to save your profile) <input type="password" name="pw"
        autocomplete="new-password"></label>
      <button type="button" onclick="location.href = '/apply/42/portal'">Submit application</button>
      </body></html>"""
    context.route("https://careers.fabrikam.example/**", lambda route: route.fulfill(
        body=portal if route.request.url.endswith("/portal") else form.replace(
            "__RESUME__", '<label>Resume * <input type="file" name="resume" required></label>'),
        content_type="text/html"))
    _enqueue(job_folder, _COMBINED_URL)
    runner = _runner(context, tmp_path, auto_apply_submit=True)
    runner.jev = type("Judge", (_FormAsAccountJudge,),
                      {"STATE": "application_form", "CONF": 0.9})()
    out = runner.drain(cap=1)[0]
    assert typed == [1], out
    assert out.status == "submitted", out
    assert out.reason.startswith("submitted (unconfirmed)"), out


_SIGN_UP_AND_APPLY = """<!doctype html><html><head><title>Create account</title></head><body>
<label>First name * <input name="first" required></label>
<label>Email * <input type="email" name="email" required></label>
<label>LinkedIn profile * <input type="url" name="linkedin" required></label>
<label>Password * <input type="password" name="pw" autocomplete="new-password" required></label>
<button type="button" onclick="location.href = '/apply/42/form'">Create account and apply</button>
</body></html>"""


def test_a_form_after_an_account_pages_submit_waits_for_the_user(
        context, job_folder, catalog_builder, tmp_path, monkeypatch):
    # the fourth review, round 2: submitting on, the gate clicked "Create
    # account and apply", the application form came next, and the job was
    # marked submitted with nothing sent. Going on instead sent a real
    # application twice when its page reset itself to the form (round 3):
    # the run can tell neither, so the job waits for the user
    typed = _count_password_fills(monkeypatch)
    context.route("https://careers.fabrikam.example/**", lambda route: route.fulfill(
        body=_THE_FORM if route.request.url.endswith("/form") else _SIGN_UP_AND_APPLY,
        content_type="text/html"))
    _enqueue(job_folder, _COMBINED_URL)
    runner = _runner(context, tmp_path, auto_apply_submit=True)
    runner.jev = type("Judge", (_FormAsAccountJudge,), {"STATE": "signup_form", "CONF": 0.9})()
    out = runner.drain(cap=1)[0]
    record = Path(out.record_path).read_text(encoding="utf-8")
    assert typed == [1], out
    assert out.status == "needs_human", out
    assert out.reason.startswith("after the account page's submit the page reads as "), out
    assert "the click may only have made the account" in out.reason, out
    assert record.count("SUBMIT CLICKED") == 1


def test_a_submit_the_guard_stopped_sent_nothing_and_parks(
        context, job_folder, catalog_builder, tmp_path, monkeypatch):
    # the fourth review, round 2: the submit button's own formaction posted
    # off the sites, the guard stopped the post, and the job read "submitted"
    _count_password_fills(monkeypatch)
    hits = []
    context.route("https://collector.example.net/**",
                  lambda route: hits.append(1) or route.fulfill(body="taken"))
    _serve_combined(context, "", resume=True, html="""<!doctype html><html><body>
      <form method="post">
        <label>First name * <input name="first" required></label>
        __RESUME__
        <label>Password * <input type="password" name="pw" autocomplete="new-password"
          required></label>
        <button formaction="https://collector.example.net/take">Submit application</button>
      </form></body></html>""")
    _enqueue(job_folder, _COMBINED_URL)
    runner = _runner(context, tmp_path, auto_apply_submit=True)
    runner.jev = type("Judge", (_FormAsAccountJudge,),
                      {"STATE": "application_form", "CONF": 0.9})()
    out = runner.drain(cap=1)[0]
    assert hits == []
    assert (out.status, out.reason) == (
        "needs_human", "the form posts to collector.example.net, outside the allowed sites; "
                       "the run stopped it and nothing was sent"), out


def test_a_submit_that_reached_the_site_before_the_guard_stopped_a_post_never_reads_unsent(
        context, job_folder, catalog_builder, tmp_path, monkeypatch):
    # final review A-I3: the submit's script posts the application to the
    # site's own API, then submits a hidden form off the sites. The guard
    # stops that post; the application went already, so the job never reads
    # "nothing was sent" (a Re-queue would send it twice)
    _count_password_fills(monkeypatch)
    hits: list = []
    api: list = []
    context.route("https://collector.example.net/**",
                  lambda route: hits.append(1) or route.fulfill(body="taken"))
    _serve_combined(context, "", resume=True, html="""<!doctype html><html><body>
      <form id="app" onsubmit="return false">
        <label>First name * <input name="first" required></label>
        __RESUME__
        <label>Password * <input type="password" name="pw" autocomplete="new-password"
          required></label>
        <button type="button" id="go">Submit application</button>
      </form>
      <form id="out" method="post" action="https://collector.example.net/take">
        <input type="hidden" name="x" value="1"></form>
      <script>document.getElementById('go').onclick = function () {
        fetch('/api/applications', {method: 'POST', body: '{}'}).then(function () {
          document.getElementById('out').submit();
        });
      };</script></body></html>""")
    context.route("https://careers.fabrikam.example/api/**", lambda route: (
        api.append(route.request.method),
        route.fulfill(body="{}", content_type="application/json")))
    _enqueue(job_folder, _COMBINED_URL)
    runner = _runner(context, tmp_path, auto_apply_submit=True)
    runner.jev = type("Judge", (_FormAsAccountJudge,),
                      {"STATE": "application_form", "CONF": 0.9})()
    out = runner.drain(cap=1)[0]
    assert api == ["POST"] and hits == []
    assert "nothing was sent" not in out.reason, out
    assert out.status in ("needs_human", "submitted"), out
    assert out.reason.endswith("; the run stopped a post to collector.example.net"), out
    if out.status == "needs_human":
        assert apply_queue.load()["jobs"][-1]["tab_note"] == apply_run.CHECK_SENT_NOTE


def test_the_account_step_never_types_the_password_into_a_masked_sensitive_box(
        context, job_folder, catalog_builder, tmp_path, monkeypatch):
    typed = _count_password_fills(monkeypatch)
    _serve_combined(context, "Create account", html="""<!doctype html><html><body>
      <label>Email * <input type="email" name="email" required></label>
      <label>Password * <input type="password" name="pw" required></label>
      <label>Social Security Number * <input type="password" name="ssn" required></label>
      <button type="button" onclick="document.body.dataset.submitted = 1">__BUTTON__</button>
      </body></html>""")
    _enqueue(job_folder, _COMBINED_URL)
    runner = _runner(context, tmp_path, auto_apply_submit=False)
    runner.jev = type("Judge", (_FormAsAccountJudge,), {"STATE": "signup_form", "CONF": 0.9})()
    out = runner.drain(cap=1)[0]
    assert typed == [], out
    assert out.status == "needs_human", out
    assert "Social Security Number" in out.reason and "never fills" in out.reason, out


@pytest.mark.parametrize("label, autocomplete, ok", [
    ("Password", "", True), ("Confirm password", "", True), ("Re-enter Password", "", True),
    ("Verify New Password", "", True), ("Password verification", "", True),
    ("Choose a code", "new-password", True),
    ("One-time password", "", False), ("Passcode", "", False), ("Passport number", "", False),
    ("Secret answer", "", False), ("Verification code", "current-password", False),
    ("Social Security Number", "new-password", False)])
def test_names_password_takes_only_an_accounts_password_box(label, autocomplete, ok):
    field = apply_form.Field(0, (0, "#f"), label, "other", True, autocomplete=autocomplete)
    assert apply_run._names_password(field) is ok


@pytest.mark.parametrize("required, stored, outcome", [
    (True, False, "the form asks for a password and no master password is stored"),
    (False, False, "ready_to_submit"),
    (False, True, "ready_to_submit")])
def test_a_password_box_the_run_cannot_fill_parks_only_when_it_is_required(
        context, job_folder, catalog_builder, tmp_path, monkeypatch, required, stored,
        outcome):
    if stored:
        typed = _count_password_fills(monkeypatch)
    html = _COMBINED if required else _COMBINED.replace(
        'Password * <input type="password" name="pw" autocomplete="new-password" required>',
        'Password <input type="password" name="pw" autocomplete="new-password">')
    assert required or "required>" not in html.split("Password", 1)[1].split("</label>")[0]
    _serve_combined(context, "Submit application", resume=True, html=html)
    _enqueue(job_folder, _COMBINED_URL)
    out = _runner(context, tmp_path, auto_apply_submit=False).drain(cap=1)[0]
    if outcome == "ready_to_submit":
        assert out.status == "ready_to_submit", out
        page = next(p for p in context.pages if not p.is_closed())
        assert page.locator("[name=pw]").input_value() == ("synthetic-password" if stored else "")
        if stored:
            assert typed == [1]
    else:
        assert (out.status, out.reason) == ("needs_human", outcome), out


def _routed_unit_run(context, tmp_path, job_folder, html, **settings):
    """A `_JobRun` on `html` served at the application's own URL."""
    context.route("https://careers.fabrikam.example/**",
                  lambda route: route.fulfill(body=html, content_type="text/html"))
    run = _unit_run(context, tmp_path, job_folder, "", **settings)
    run.page.goto("https://careers.fabrikam.example/jobs/42")
    return run


@pytest.mark.parametrize("html, where, reason", [
    # the page is not the application's site (`about:blank`)
    ('<label>Password <input type="password" name="pw" required></label>', "blank",
     "a password box on about, outside the application site"),
    # (a text box the planner read as a password by its autocomplete: since
    # final review B-I2 only a masked input is one,
    # `test_only_a_masked_input_is_a_password_box`)
    # a masked one-time code or security answer is no account password
    ('<label>One-time password <input type="password" name="otp" required></label>', "site",
     "One-time password is not a password box for an account"),
    ('<label>Secret answer <input type="password" name="answer" required></label>', "site",
     "Secret answer is not a password box for an account"),
    # the box's form would post it off the allowed sites
    ('<form action="https://collector.example.net/take" method="post"><label>Password '
     '<input type="password" name="pw" required></label><button>Next</button></form>', "site",
     "the password box's form posts to collector.example.net, outside the allowed sites"),
    # final review A-M1: LinkedIn may load in the tab and never takes the password
    ('<form action="https://www.linkedin.com/take" method="post"><label>Password '
     '<input type="password" name="pw" required></label><button>Next</button></form>', "site",
     "the password box's form posts to www.linkedin.com, outside the allowed sites")])
def test_the_master_password_goes_only_into_a_password_input_on_the_application_site(
        context, job_folder, catalog_builder, tmp_path, monkeypatch, html, where, reason):
    typed = _count_password_fills(monkeypatch)
    if where == "blank":
        run = _unit_run(context, tmp_path, job_folder, f"<body>{html}</body>")
    else:
        run = _routed_unit_run(context, tmp_path, job_folder, f"<body>{html}</body>")
    digest = apply_form.extract(run.page)
    plan = apply_judge.plan(digest, run.catalog, {})
    assert [pf.action for pf in plan.fields] == [apply_judge.PASSWORD_ACTION]
    with pytest.raises(apply_run._Parked, match=re.escape(reason)):
        with run._password_guard() as guard:
            run._fill_passwords(digest, plan, run.pages[-1], guard)
    assert typed == []


@pytest.mark.parametrize("html, planned", [
    # final review B-I2: sites put autocomplete="new-password" on an ordinary
    # box to stop the browser's autofill; that box takes its fact
    ('<label>City * <input type="text" name="city" autocomplete="new-password" required>'
     '</label>', "fact"),
    ('<label for="loc">Location *</label><input id="loc" role="combobox" aria-expanded="false" '
     'autocomplete="new-password" required>', "fact"),
    # a masked box is a password box whatever its words
    ('<label>PIN * <input type="password" name="pin" required></label>', "password")])
def test_only_a_masked_input_is_a_password_box(
        context, job_folder, catalog_builder, tmp_path, html, planned):
    run = _routed_unit_run(context, tmp_path, job_folder, f"<body>{html}</body>")
    digest = apply_form.extract(run.page)
    assert len(digest.fields) == 1, digest.fields
    answers = {"field_0_source": jev.Answer(kind="choice", choice="location",
                                            probabilities={"location": 0.95}, confidence=0.95)}
    assert run.catalog.has("location")
    pf = apply_judge.plan(digest, run.catalog, answers).fields[0]
    if planned == "fact":
        assert pf.fact_key and pf.action not in ("skip", apply_judge.PASSWORD_ACTION), pf
        assert apply_judge.page_facts(digest).passwords == 0, digest.fields
    else:
        assert (pf.action, pf.fact_key) == (apply_judge.PASSWORD_ACTION, None), pf


def test_a_control_read_without_its_secret_keeps_a_masked_box_a_password_box():
    # final review B R2 nit: an older capture's field (no `secret` key) and a
    # hand-built `Field` are password boxes when their ident names a masked
    # input; a text box with a password's autocomplete is still none
    masked = "input|password|pw|pw||||||password"
    digest = apply_form.FormDigest.from_dict({
        "url_host": "x.example", "title": "Sign in", "text": "", "fields": [
            {"n": 0, "locator": [0, "#pw"], "label": "Password", "type": "other",
             "ident": masked},
            {"n": 1, "locator": [0, "#city"], "label": "City", "type": "text",
             "autocomplete": "new-password", "ident": "input|text|city|city||||||city"}]})
    assert [f.secret for f in digest.fields] == [True, False]
    assert [apply_form.password_box(f) for f in digest.fields] == [True, False]
    built = apply_form.Field(0, (0, "#pw"), "Password", "other", True, ident=masked)
    assert apply_form.password_box(built)
    assert not apply_form.password_box(apply_form.Field(0, (0, "#q"), "Password hint", "other",
                                                        False, ident="input|text|q|q||||||hint"))


@pytest.mark.parametrize("html", [
    '<label>Password <input type="PASSWORD" name="pw" required></label>',
    '<form action="javascript:void(0)"><label>Password <input type="password" name="pw" '
    'required></label></form>'])
def test_a_password_box_on_the_site_takes_the_master_password(
        context, job_folder, catalog_builder, tmp_path, monkeypatch, html):
    # an upper-case type is a password input; a `javascript:` action goes nowhere
    typed = _count_password_fills(monkeypatch)
    run = _routed_unit_run(context, tmp_path, job_folder, f"<body>{html}</body>")
    digest = apply_form.extract(run.page)
    plan = apply_judge.plan(digest, run.catalog, {})
    with run._password_guard() as guard:
        run._fill_passwords(digest, plan, run.pages[-1], guard)
    assert typed == [1]
    assert run.page.locator("[name=pw]").input_value() == "synthetic-password"


def test_the_password_guard_stops_a_page_leaving_the_sites_and_lets_go_after(
        context, job_folder, catalog_builder, tmp_path, monkeypatch):
    _count_password_fills(monkeypatch)
    run = _routed_unit_run(context, tmp_path, job_folder, """<body>
      <label>Password * <input type="password" name="pw" required></label>
      <button id="go" onclick="location.href = 'https://collector.example.net/take'">Next</button>
      </body>""")
    hits = []
    context.route("https://collector.example.net/**",
                  lambda route: hits.append(1) or route.fulfill(body="taken"))
    digest = apply_form.extract(run.page)
    plan = apply_judge.plan(digest, run.catalog, {})
    with pytest.raises(apply_run._Parked, match="left the allowed sites: collector.example.net"):
        with run._password_guard() as guard:
            run._fill_passwords(digest, plan, run.pages[-1], guard)
            run.page.click("#go")
            run.page.wait_for_timeout(300)
    assert hits == []
    # the route is gone once the step ends: the window is the user's again,
    # and the same navigation goes through
    assert guard._on is False
    run.page.goto("https://collector.example.net/take")
    assert hits == [1]


def test_the_password_guard_stops_a_post_to_linkedin_a_get_may_load(
        context, job_folder, catalog_builder, tmp_path, monkeypatch):
    # final review A-M1: LinkedIn is an allowed site for a load and never the
    # application's site, so a form post there with the password is stopped
    _count_password_fills(monkeypatch)
    run = _routed_unit_run(context, tmp_path, job_folder, """<body>
      <label>Password * <input type="password" name="pw" required></label>
      <button id="go" onclick="var f = document.createElement('form'); f.method = 'post';
        f.action = 'https://www.linkedin.com/checkpoint'; document.body.appendChild(f);
        f.submit()">Next</button>
      </body>""")
    hits: list = []
    context.route("https://www.linkedin.com/**",
                  lambda route: hits.append(route.request.method) or route.fulfill(body="in"))
    digest = apply_form.extract(run.page)
    plan = apply_judge.plan(digest, run.catalog, {})
    with pytest.raises(apply_run._Parked, match=re.escape(
            "the form posts to www.linkedin.com, outside the allowed sites; the run stopped "
            "it and nothing was sent")):
        with run._password_guard() as guard:
            run._fill_passwords(digest, plan, run.pages[-1], guard)
            run.page.click("#go")
            run.page.wait_for_timeout(300)
    assert hits == []
    assert guard.posts == ["POST https://www.linkedin.com/checkpoint"]


def test_sends_application_reads_the_button_text():
    texts = ["Submit application", "Apply", "Send", "Create account and apply",
             "Register and submit", "Sign in to apply", "Sign in", "Next",
             "Verify and continue", "Send code", "Send me a link", "Create account"]
    digest = apply_form.FormDigest(url_host="x", title="t", text="", buttons=[
        apply_form.Button(n, (0, f"#b{n}"), text, "") for n, text in enumerate(texts)])
    assert [apply_run._sends_application(digest, n) for n in range(len(texts))] == [
        True, True, True, True, True, False, False, False, False, False, False, False]
    # with boxes beyond the address and the password, a sign-in word no longer excuses it
    assert apply_run._sends_application(digest, texts.index("Sign in to apply"),
                                        account_only=False)
    # a last step's word sends too, unless it names the account itself
    finals = apply_form.FormDigest(url_host="x", title="t", text="", buttons=[
        apply_form.Button(n, (0, f"#f{n}"), text, "") for n, text in enumerate(
            ["Complete application", "Confirm", "Complete registration", "Done",
             "Complete profile"])])
    assert [apply_run._sends_application(finals, n) for n in range(5)] == [
        True, True, False, True, False]


def _unit_run(context, tmp_path, job_folder, html, **settings):
    """A `_JobRun` on `html` (set_content), prepared, with one page record."""
    _enqueue(job_folder, "https://careers.fabrikam.example/jobs/42")
    run = apply_run._JobRun(_runner(context, tmp_path, **settings), context, _entry())
    run._prepare()
    run.page = context.new_page()
    run.page.set_content(html)
    run._new_page_record("x", 1.0)
    return run


def test_a_code_gate_before_any_submit_never_clicks_a_button_that_sends(
        context, job_folder, catalog_builder, tmp_path, monkeypatch):
    run = _unit_run(context, tmp_path, job_folder, """
      <body><label for="code">Verification code</label><input id="code">
        <button id="go" onclick="document.body.dataset.submitted = 1">Submit application</button>
      </body>""")
    monkeypatch.setattr(run.inbox, "fetch_code", lambda *a: "123456")
    digest = apply_form.extract(run.page)
    plan = FillPlan(buttons={"submit": (0, 0.95)})
    with pytest.raises(apply_run._Parked, match="would send the application"):
        run._code_gate(digest, plan, run.pages[-1])
    assert run.page.locator("body[data-submitted]").count() == 0


@pytest.mark.parametrize("sends, read, status", [
    (0.9, "review_page", "ready_to_submit"), (0.1, "review_page", "needs_human"),
    # the live judge read a form's own "Apply" and a signup form's "Create
    # account and apply" as sending at 0.67 and 0.48, and every posting's
    # Apply entry at 0.15 or less (SP8b)
    (0.48, "review_page", "ready_to_submit"), (0.30, "review_page", "needs_human"),
    # the controller's ruling: a fieldless page's Apply after an earlier fill
    # counts only on a page read as a review or a form
    (0.9, "job_posting", "needs_human")])
def test_a_posting_read_after_a_filled_form_goes_to_the_submit_gate(
        context, job_folder, catalog_builder, tmp_path, sends, read, status):
    # SP3 (INV-01): its Apply is the submit only with the judge's word that
    # it sends the finished application (`button_{n}_sends`)
    run = _unit_run(context, tmp_path, job_folder, """
      <body><h1>Your application</h1>
        <button id="go" onclick="document.body.dataset.submitted = 1">Apply</button></body>""",
                    auto_apply_submit=False)
    run.form_filled = True
    run._last_answers = {"button_0_sends": jev.Answer(kind="noul", noul=sends),
                         "page_state": jev.Answer(kind="choice", choice=read, confidence=0.9,
                                                  probabilities={read: 0.9})}
    digest = apply_form.extract(run.page)
    plan = FillPlan(buttons={"apply_entry": (0, 0.95)})
    with pytest.raises(apply_run._Parked) as parked:
        run._job_posting(digest, {}, plan, run.pages[-1])
    assert parked.value.status == status, parked.value.reason
    assert run.page.locator("body[data-submitted]").count() == 0


# The digest-based LinkedIn shortcut (`_linkedin_apply`) is gone: a LinkedIn
# job page is read by `apply_linkedin` whatever fields or buttons sit beside
# its Apply (tests/test_apply_entry.py).


class _PostingJudge(jev.FakeJev):
    """The fake, with every page that carries fields judged `job_posting`."""

    def judge(self, state, questions):
        out = super().judge(state, questions)
        if "page_state" in out and state.get("fields"):
            out["page_state"] = jev.Answer(kind="choice", choice="job_posting",
                                           probabilities={"job_posting": 1.0}, confidence=1.0)
        return out


def test_job_posting_with_a_form_is_filled_and_its_apply_button_is_not_clicked(
        context, fixture_url, job_folder, catalog_builder, tmp_path):
    _enqueue(job_folder, fixture_url("posting_with_form.html"))
    runner = _runner(context, tmp_path, auto_apply_submit=False)
    runner.jev = _PostingJudge()
    out = runner.drain(cap=1)[0]
    assert out.status == "ready_to_submit", out
    page = next(p for p in context.pages if not p.is_closed())
    assert page.evaluate("window.__clicks") == 0
    assert page.locator("body[data-submitted]").count() == 0
    assert page.locator("#first_name").input_value() == "Jane"
    record = Path(out.record_path).read_text(encoding="utf-8")
    assert "State: job_posting" in record and "(apply_entry)" not in record


# --- can_submit: every failing reason ---------------------------------------------------

def _pf(n, label, required=True, action="fill"):
    return PlannedField(n=n, locator=(0, f"#f{n}"), label=label, required=required,
                        fact_key="first_name", value="Jane", option=None, confidence=0.9,
                        action=action)


def _ok_plan():
    return FillPlan(fields=[_pf(0, "First name"), _pf(1, "Nickname", required=False,
                                                       action="skip")],
                    buttons={"submit": (3, 0.95), "back": (2, 0.9)},
                    flags={"asks_for_prohibited": 0.1, "requires_account": 0.1,
                           "has_captcha": 0.1})


def _ok_verification():
    return [VerifyResult(n=0, label="First name", ok=True, p_correct=0.9, p_placeholder=0.1)]


_ON = {"auto_apply_submit": True}


def test_can_submit_passes_the_clean_case():
    assert apply_run.can_submit(_ok_plan(), _ok_verification(), _ON) == (True, "")


def test_can_submit_setting_off():
    ok, why = apply_run.can_submit(_ok_plan(), _ok_verification(), {"auto_apply_submit": False})
    assert (ok, why) == (False, "auto_apply_submit is off")


def test_can_submit_park_reason():
    plan = _ok_plan()
    plan.park_reason = "required field without an answer: SSN"
    assert apply_run.can_submit(plan, _ok_verification(), _ON) == (False, plan.park_reason)


def test_can_submit_required_field_without_an_answer():
    """A required field whose action is anything other than fill / select /
    upload (a skip, or a `generate` nobody resolved) blocks the submit."""
    for action in ("skip", "generate", "other"):
        plan = _ok_plan()
        plan.fields[1].required = True
        plan.fields[1].action = action
        assert apply_run.can_submit(plan, _ok_verification(), _ON) == (
            False, "required field without an answer: Nickname"), action
    plan = _ok_plan()
    plan.fields[1].action = "generate"                  # optional: no bar
    assert apply_run.can_submit(plan, _ok_verification(), _ON) == (True, "")


def test_can_submit_required_field_unverified_or_failed():
    ok, why = apply_run.can_submit(_ok_plan(), [], _ON)
    assert (ok, why) == (False, "required field not verified: First name")
    bad = [VerifyResult(n=0, label="First name", ok=False, p_correct=0.4, p_placeholder=0.1)]
    ok, why = apply_run.can_submit(_ok_plan(), bad, _ON)
    assert not ok and why.startswith("required field failed verification: First name")


def test_can_submit_ignores_the_recorded_flags():
    plan = _ok_plan()
    plan.flags["asks_for_prohibited"] = 0.9
    plan.flags["has_captcha"] = 0.9
    assert apply_run.can_submit(plan, _ok_verification(), _ON) == (True, "")


def test_can_submit_submit_button():
    plan = _ok_plan()
    del plan.buttons["submit"]
    assert apply_run.can_submit(plan, _ok_verification(), _ON) == (False, "no submit button")
    plan = _ok_plan()
    plan.buttons["submit"] = (3, 0.45)
    assert apply_run.can_submit(plan, _ok_verification(), _ON) == (
        False, "submit button confidence 0.45 below 0.50")
    # the live judge's lowest reads of a true submit (the single-page form's
    # "Submit application": 0.70 and 0.74 in the first recording, 0.60 in
    # the second, 2026-09-25) clear the gate
    for conf in (0.60, 0.70):
        plan.buttons["submit"] = (3, conf)
        assert apply_run.can_submit(plan, _ok_verification(), _ON) == (True, "")


# --- the review page resolves generation before the gate ---------------------------------

def test_review_page_resolves_generation_then_fills_and_verifies_before_the_gate(
        context, job_folder, tmp_path, catalog_builder, monkeypatch):
    import apply_form
    e = _enqueue(job_folder, "https://boards.greenhouse.io/acme/jobs/1")
    run = apply_run._JobRun(_runner(context, tmp_path), context, e)
    run._prepare()
    essay = apply_form.Field(n=0, locator=(0, "#why"), label="Your motivation for this role",
                             type="textarea", required=True, help="Max 500 characters.")
    digest = apply_form.FormDigest(url_host="boards.greenhouse.io", title="Review",
                                   text="Review your application", fields=[essay],
                                   buttons=[apply_form.Button(0, (0, "#go"), "Submit", "submit")])
    plan = FillPlan(fields=[PlannedField(n=0, locator=essay.locator, label=essay.label,
                                         required=True, fact_key="needs_generation", value="",
                                         option=None, confidence=0.9, action="generate")],
                    buttons={"submit": (0, 0.95)},
                    flags={"asks_for_prohibited": 0.1, "requires_account": 0.1,
                           "has_captcha": 0.1})
    rec = {"url": "https://boards.greenhouse.io/acme/review", "state": "review_page",
           "confidence": 0.9, "filled": [], "verification": [], "clicked": [], "flags": {}}
    with pytest.raises(apply_run._Parked) as info:
        run._review_page(digest, {}, plan, rec)
    assert info.value.status == "needs_human"
    assert info.value.reason == "required field without an answer: Your motivation for this role"
    missing = _entry()["missing_answers"]
    assert [(m["question"], m["context"]) for m in missing] == [
        ("Your motivation for this role", "Max 500 characters.")]

    class Gen:
        def answer(self, field, catalog, judge, *, budget):
            return "Two years of ingestion pipelines at Acme Corp."
    run.r.answergen = Gen()
    plan.fields[0].action = "generate"
    plan.missing.clear()
    plan.park_reason = ""
    filled = [apply_run.apply_fill.Filled(0, essay.label,
                                          "Two years of ingestion pipelines at Acme Corp.")]
    verified = [VerifyResult(0, essay.label, True, 0.99, 0.01)]
    monkeypatch.setattr(apply_run.apply_fill, "apply", lambda *a, **kw: filled)
    monkeypatch.setattr(run, "_verify",
                        lambda actual, drafts=None, picks=None, shaped=None: verified)
    monkeypatch.setattr(run, "_retry_failed",
                        lambda p, actual, checks, drafts=None, shaped=None: checks)
    gated = []
    monkeypatch.setattr(run, "_submit_gate",
                        lambda d, p, checks, r: gated.append((d, p, checks, r)))

    run._review_page(digest, {}, plan, rec)

    assert gated == [(digest, plan, verified, rec)]
    assert rec["filled"][0]["value"] == "Two years of ingestion pipelines at Acme Corp."
    assert rec["verification"][0]["ok"] is True


def test_runner_discovers_dynamic_listbox_options_before_planning(
        context, fixture_url, job_folder, catalog_builder, tmp_path):
    _enqueue(job_folder, fixture_url("generic_listbox.html"))
    seen = []

    class InspectingJev(jev.FakeJev):
        def judge(self, state, questions):
            if any(q.startswith("field_") for q in questions):
                # the mapping (SP4: asked after the page read) carries the options
                country = next((f for f in state.get("fields", [])
                                if f.get("id_or_name") == "country"), None)
                if country is not None:
                    seen.append(country.get("options", []))
            return super().judge(state, questions)

    runner = _runner(context, tmp_path, auto_apply_submit=False)
    runner.jev = InspectingJev()
    out = runner.drain(cap=1)[0]
    assert out.status == "needs_human", out
    assert seen and seen[0] == ["United States", "Canada", "Other"]
    page = next(p for p in context.pages if not p.is_closed())
    assert page.get_attribute("#country", "data-value") == "United States"


def test_verify_uses_complete_catalog_evidence(context, job_folder, tmp_path):
    class CapturingJev:
        state = None

        def judge(self, state, questions):
            self.state = state
            return {qid: jev.Answer(kind="noul", noul=0.9) for qid in questions}

    judge = CapturingJev()
    e = apply_queue.new_entry("42", apply_url="https://boards.greenhouse.io/acme/jobs/1")
    run = apply_run._JobRun(
        apply_run.Runner(jev=judge, context=context, run_context=_RUN_CONTEXT,
                         sleep=_no_sleep), context, e)
    run.catalog = type("Catalog", (), {
        "verification_excerpt": lambda self: "complete catalog evidence",
        "sheet_excerpt": lambda self: "truncated prose",
    })()

    run._verify([apply_run.apply_fill.Filled(0, "Email", "jane.doe@example.com")])

    assert judge.state["sheet_excerpt"] == "complete catalog evidence"


# --- write_record: the hidden rule ---------------------------------------------------------

def test_write_record_hides_a_password_field_value(tmp_path):
    entry = apply_queue.new_entry("7", company="Acme", title="Engineer",
                                  apply_url="https://jobs.example.com/7")
    pages = [{"url": "https://jobs.example.com/login", "state": "login_wall",
              "confidence": 0.9, "clicked": ["Sign in (advance)"], "flags": {},
              "filled": [{"n": 0, "label": "Email", "value": "jane@example.com",
                          "type": "email", "id_or_name": "login_email", "upload": False},
                         {"n": 1, "label": "Password", "value": "hunter2",
                          "type": "other", "id_or_name": "login_password", "upload": False},
                         {"n": 2, "label": "", "value": "hunter2",
                          "type": "other", "id_or_name": "passwd", "upload": False},
                         {"n": 3, "label": "Passport number", "value": "unlikely",
                          "type": "text", "id_or_name": "doc", "upload": False},
                         {"n": 4, "label": "Resume", "value": "Jane.pdf",
                          "type": "file", "id_or_name": "resume", "upload": True}],
              "verification": [{"n": 0, "label": "Email", "ok": True,
                                "p_correct": 0.9, "p_placeholder": 0.1}]}]
    path = apply_run.write_record(tmp_path, entry, "needs_human", "login wall", pages,
                                  {"requests": 2, "input_tokens": 1234, "usd": 0.0000518},
                                  "Sign in to continue",
                                  missing=[{"question": "Nickname", "context": "text"}])
    text = path.read_text(encoding="utf-8")
    assert path == tmp_path / "apply_record.md"
    assert "hunter2" not in text
    assert "- Password: <hidden>" in text and "- : <hidden>" in text
    assert "- Passport number: unlikely" in text          # a text control is not a password
    assert "- Uploads:\n  - Resume: Jane.pdf" in text
    assert "- Email: ok (p_correct 0.90, p_placeholder 0.10)" in text
    assert "- Clicked: Sign in (advance)" in text
    assert "- Nickname (text)" in text
    assert "- Requests: 2\n- Input tokens: 1234\n- Cost: $0.0001\n- Model: jev-1.13.0" in text
    assert "Sign in to continue" in text
    assert chr(0x2014) not in text
    pages[0]["filled"].append({"n": 5, "label": "Security code", "value": "MKPZ3QRA",
                               "type": "text", "id_or_name": "code", "hidden": True})
    text = apply_run.write_record(tmp_path, entry, "needs_human", "x", pages, {}, "",
                                  missing=[]).read_text(encoding="utf-8")
    assert "- Security code: <hidden>" in text and "MKPZ3QRA" not in text


# --- _finish survives a failing queue write ------------------------------------------------

def test_finish_retries_the_queue_write_once_after_a_second(
        context, fixture_url, job_folder, catalog_builder, tmp_path, monkeypatch):
    _enqueue(job_folder, fixture_url("captcha.html"))
    real = apply_run.apply_queue.finish
    calls = []

    def _flaky(*a, **kw):
        calls.append(a)
        if len(calls) == 1:
            raise apply_queue.QueueLockTimeout("held elsewhere")
        return real(*a, **kw)
    monkeypatch.setattr(apply_run.apply_queue, "finish", _flaky)
    naps = []
    runner = _runner(context, tmp_path)
    runner.sleep = naps.append
    out = runner.drain(cap=1)[0]
    assert out.status == "needs_human"
    # the one finish retry waits FINISH_RETRY_S; the other naps are the headless
    # captcha page's moment to clear itself (HUMAN_CHECK_POLL_S each)
    assert len(calls) == 2 and naps[-1] == apply_run.FINISH_RETRY_S
    assert naps.count(apply_run.FINISH_RETRY_S) == 1
    assert _entry()["status"] == "needs_human"


def test_finish_logs_an_error_naming_the_job_and_the_drain_continues(
        context, fixture_url, job_folder, catalog_builder, tmp_path, monkeypatch, caplog):
    _enqueue(job_folder, fixture_url("captcha.html"), jid="a")
    _enqueue(job_folder, fixture_url("captcha.html"), jid="b")
    real = apply_run.apply_queue.finish

    def _broken(job_id, *a, **kw):
        if job_id == "a":
            raise OSError("disk full")
        return real(job_id, *a, **kw)
    monkeypatch.setattr(apply_run.apply_queue, "finish", _broken)
    runner = _runner(context, tmp_path)
    runner.sleep = lambda s: None
    with caplog.at_level("ERROR", logger="apply_run"):
        outcomes = runner.drain(cap=5)
    assert [o.job_id for o in outcomes] == ["a", "b"]
    assert [o.status for o in outcomes] == ["needs_human", "needs_human"]
    assert any("job a" in r.getMessage() and "disk full" in r.getMessage()
               for r in caplog.records if r.levelname == "ERROR")
    assert _entry("a")["status"] == "in_progress"       # the write never landed
    assert _entry("b")["status"] == "needs_human"


def test_is_password_covers_names_words_and_autocomplete():
    hidden = [
        {"type": "other", "id_or_name": "login_password", "label": ""},
        {"type": "other", "id_or_name": "pwd", "label": ""},
        {"type": "other", "id_or_name": "", "label": "Account secret"},
        {"type": "other", "id_or_name": "user_pass", "label": ""},
        {"type": "text", "id_or_name": "x", "label": "", "autocomplete": "current-password"},
        {"type": "other", "id_or_name": "x", "label": "", "autocomplete": "new-password"},
    ]
    shown = [
        {"type": "text", "id_or_name": "passport", "label": "Passport number"},
        {"type": "text", "id_or_name": "secret_santa", "label": ""},
        {"type": "other", "id_or_name": "otp", "label": "Security code",
         "autocomplete": "one-time-code"},
        {"type": "email", "id_or_name": "login_email", "label": "Email",
         "autocomplete": "username"},
    ]
    assert all(apply_run._is_password(r) for r in hidden), hidden
    assert not any(apply_run._is_password(r) for r in shown), shown


# --- the hooks' defaults ------------------------------------------------------------------

def test_not_configured_hooks_answer_cannot():
    h = apply_run.NotConfigured()
    assert h.login(None, None, "x") is False and h.signup(None, None, "x") is False
    assert h.fetch_code(None, "x", "https://mail") is None
    assert h.answer(None, None, None, budget=3) is None


# --- hold: waits for the window to close --------------------------------------------------

def test_hold_returns_when_every_page_is_closed(tmp_path):
    class Ctx:
        def __init__(self):
            self.pages = [1, 2]
            self.handlers = {}

        def on(self, event, fn):
            self.handlers[event] = fn

    ctx = Ctx()
    naps = []

    def _sleep(s):
        naps.append(s)
        ctx.pages.pop()
    runner = apply_run.Runner(jev=None, profile_dir=tmp_path, settings={}, sleep=_sleep,
                              context=ctx)
    runner._hold(ctx)
    assert naps == [apply_run.HOLD_POLL_S] * 2

    ctx = Ctx()
    naps.clear()

    def _close_then_sleep(s):
        naps.append(s)
        ctx.handlers["close"]()
    runner = apply_run.Runner(jev=None, profile_dir=tmp_path, settings={},
                              sleep=_close_then_sleep, context=ctx)
    runner._hold(ctx)
    assert naps == [apply_run.HOLD_POLL_S]


def test_hold_waits_inside_playwright_so_a_closed_window_is_seen(tmp_path):
    # Playwright's sync API dispatches a page's close event only while one of
    # its own calls runs; `time.sleep` blocks it, so a hold that sleeps never
    # sees the user close the window (the 2026-09-22 drain sat on after it)
    class Ctx:
        def __init__(self):
            self.pages = [1]
            self.waits = []

        def on(self, event, fn):
            pass

        def wait_for_event(self, event, timeout):
            self.waits.append((event, timeout))
            self.pages.clear()              # the dispatch that runs inside the call
            raise TimeoutError("Timeout 1000ms exceeded")

    def _sleep(s):
        raise AssertionError("the hold slept outside Playwright")

    ctx = Ctx()
    apply_run.hold_until_closed(ctx, sleep=_sleep)
    assert ctx.waits == [("close", apply_run.HOLD_POLL_S * 1000)]


def test_hold_ends_when_a_real_page_closes_on_its_own(_browser):
    ctx = _browser.new_context()
    try:
        page = ctx.new_page()
        page.evaluate("setTimeout(() => window.close(), 100)")
        naps = []

        def _sleep(s):
            naps.append(s)
            if len(naps) > 5:
                raise AssertionError("the hold never saw the page close")
        apply_run.hold_until_closed(ctx, sleep=_sleep)
        assert ctx.pages == []
    finally:
        ctx.close()


# --- the CLI ----------------------------------------------------------------------------------

@pytest.fixture
def hermetic_cli(monkeypatch):
    """The CLI never reads the developer's .env or config: `_load_env` is a
    no-op and `load_settings` returns the defaults (a test may override)."""
    import settings
    monkeypatch.setattr(apply_run, "_load_env", lambda: None)
    monkeypatch.setattr(apply_run, "load_settings", lambda: dict(apply_run.DEFAULT_SETTINGS))

    def _never(*a, **kw):
        raise AssertionError("the real settings store was read")
    monkeypatch.setattr(settings, "load", _never)
    monkeypatch.setattr(settings, "secret_status", _never)
    return monkeypatch


def test_main_prints_an_errors_type_and_step_never_its_message(hermetic_cli, monkeypatch,
                                                               capsys, caplog):
    # final review A-M6 (RES-05): a Playwright message carries the page's
    # words and the values typed; the frames go to the log
    monkeypatch.setattr(apply_run.jev, "get", lambda mode="": jev_harness.judge())
    message = "Locator.fill: typed 'synthetic-typed-value' into #email"

    class R:
        def __init__(self, **kw):
            pass

        def drain(self, cap):
            raise RuntimeError(message)
    monkeypatch.setattr(apply_run, "Runner", R)
    assert apply_run.main(["drain"]) == 1
    err = capsys.readouterr().err
    assert "apply_run: error: RuntimeError at " in err, err
    assert "synthetic-typed-value" not in err and "Locator.fill" not in err, err
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "RuntimeError" in logged and "synthetic-typed-value" not in logged


def test_main_drain_exits_2_when_the_judge_is_unavailable(hermetic_cli, monkeypatch, capsys):
    def _get(mode=""):
        raise jev.JevUnavailable("No TypeSafe API key. Create one at console.typesafe.ai/keys")
    monkeypatch.setattr(apply_run.jev, "get", _get)
    claimed = []
    monkeypatch.setattr(apply_run.apply_queue, "claim",
                        lambda *a, **k: claimed.append(1) or None)
    assert apply_run.main(["drain"]) == 2
    err = capsys.readouterr().err
    assert "No TypeSafe API key" in err and "console.typesafe.ai/keys" in err
    assert claimed == []


def test_main_one_exits_2_when_the_job_is_not_queued(hermetic_cli, monkeypatch, capsys):
    monkeypatch.setattr(apply_run.jev, "get", lambda mode="": jev_harness.judge())
    assert apply_run.main(["one", "nope", "--jev", "typesafe"]) == 2
    assert "not queued" in capsys.readouterr().err


def test_main_drain_prints_the_summary_and_exits_0(hermetic_cli, monkeypatch, capsys):
    monkeypatch.setattr(apply_run.jev, "get", lambda mode="": jev_harness.judge())
    seen = {}

    class R:
        def __init__(self, **kw):
            seen.update(kw)

        def drain(self, cap):
            seen["cap"] = cap
            return [apply_run.Outcome("1", "submitted", "confirmation page", "", 3,
                                      {"requests": 4, "input_tokens": 2000, "usd": 0.000084})]
    monkeypatch.setattr(apply_run, "Runner", R)
    assert apply_run.main(["drain", "--cap", "2", "--no-submit", "--headless",
                           "--jev", "typesafe"]) == 0
    out = capsys.readouterr().out
    assert out.strip().endswith("drained 1: submitted 1, ready_to_submit 0, needs_human 0, "
                                "failed 0; Jev 4 requests, 2000 tokens, $0.0001")
    assert seen["cap"] == 2
    assert seen["settings"]["auto_apply_submit"] is False
    assert seen["settings"]["auto_apply_headless"] is True
    assert seen["settings"]["auto_apply_jev_mode"] == "typesafe"


def test_main_unexpected_error_exits_1(hermetic_cli, monkeypatch, capsys):
    monkeypatch.setattr(apply_run.jev, "get", lambda mode="": jev_harness.judge())

    class R:
        def __init__(self, **kw):
            pass

        def drain(self, cap):
            raise RuntimeError("boom")
    monkeypatch.setattr(apply_run, "Runner", R)
    assert apply_run.main(["drain", "--jev", "typesafe"]) == 1
    # the type and the step only (final review A-M6)
    err = capsys.readouterr().err
    assert "apply_run: error: RuntimeError at " in err and "boom" not in err


def test_doctor_prints_one_line_per_row_and_the_profile(tmp_path, capsys, monkeypatch):
    import settings
    import setup_check
    monkeypatch.setattr(setup_check, "module_found", lambda name: True)
    monkeypatch.setattr(setup_check, "chromium_installed", lambda: True)
    monkeypatch.setattr(setup_check, "chrome_installed", lambda: False)
    monkeypatch.setattr(settings, "load", lambda: {"auto_apply_jev_mode": "typesafe"})
    monkeypatch.setattr(settings, "secret_status", lambda: {"TYPESAFE_API_KEY": False})
    monkeypatch.setenv("TYPESAFE_API_KEY", "not-a-real-key")
    profile = tmp_path / "profile"
    assert apply_run.doctor(profile) == 0
    out = capsys.readouterr().out
    lines = out.strip().splitlines()
    assert lines[0].startswith("ok") and "TypeSafe API key" in lines[0]
    assert lines[1].startswith("ok") and "typesafe_sdk" in lines[1]
    assert lines[2].startswith("ok") and "playwright" in lines[2]
    assert lines[3].startswith("ok") and "chromium" in lines[3]
    assert "browser profile" in lines[4] and str(profile) in lines[4] and "absent" in lines[4]
    assert "not-a-real-key" not in out

    monkeypatch.delenv("TYPESAFE_API_KEY")
    monkeypatch.setattr(setup_check, "chromium_installed", lambda: False)
    profile.mkdir()
    assert apply_run.doctor(profile) == 2
    out = capsys.readouterr().out
    assert "MISSING  chromium" in out and "playwright install chromium" in out
    assert "ok       browser profile" in out

    monkeypatch.setattr(setup_check, "chrome_installed", lambda: True)
    monkeypatch.setenv("TYPESAFE_API_KEY", "not-a-real-key")
    assert apply_run.doctor(profile) == 0
    assert "ok       browser: Google Chrome" in capsys.readouterr().out


def test_doctor_reads_the_key_from_the_saved_settings_too(tmp_path, capsys, monkeypatch):
    import settings
    import setup_check
    monkeypatch.setattr(setup_check, "module_found", lambda name: True)
    monkeypatch.setattr(setup_check, "chromium_installed", lambda: True)
    monkeypatch.setattr(settings, "load", lambda: {"auto_apply_jev_mode": "typesafe"})
    monkeypatch.setattr(settings, "secret_status", lambda: {"TYPESAFE_API_KEY": True})
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    assert apply_run.doctor(tmp_path / "profile") == 0
    assert capsys.readouterr().out.startswith("ok       TypeSafe API key")


def test_main_settings_come_from_the_loader_and_flags_override(hermetic_cli, monkeypatch,
                                                                capsys):
    monkeypatch.setattr(apply_run, "load_settings",
                        lambda: {**apply_run.DEFAULT_SETTINGS, "auto_apply_batch_cap": 3,
                                 "auto_apply_jev_mode": "typesafe", "auto_apply_submit": False})
    monkeypatch.setattr(apply_run.jev, "get", lambda mode="": jev_harness.judge())
    seen = {}

    class R:
        def __init__(self, **kw):
            seen.update(kw)

        def drain(self, cap):
            seen["cap"] = cap
            return []
    monkeypatch.setattr(apply_run, "Runner", R)
    assert apply_run.main(["drain"]) == 0
    assert seen["cap"] == 3
    assert seen["settings"]["auto_apply_submit"] is False
    assert seen["settings"]["auto_apply_jev_mode"] == "typesafe"


# --- cycle 18: the store is the one source of the answers (FL-2, FL-4) ------------------

def test_prepare_refreshes_the_sheet_from_the_store_before_the_facts(
        context, job_folder, tmp_path):
    # FL-2: the sheet was written when the store said Yes; the store now says No
    apply_answers.save(standard_bank(willing_to_relocate="No", address_street="9 New Street"))
    sheet = job_folder / "apply.md"
    before = sheet.read_text(encoding="utf-8")
    assert "- **Are you willing to relocate?** Yes\n" in before
    e = _enqueue(job_folder, "https://boards.greenhouse.io/acme/jobs/1")
    run = apply_run._JobRun(_runner(context, tmp_path), context, e)
    run._prepare()
    after = sheet.read_text(encoding="utf-8")
    assert "- **Are you willing to relocate?** No\n" in after
    assert "9 New Street" in after and "123 Main Street" not in after
    assert run.catalog.facts["willing_to_relocate"].value == "No"
    # outside the two sections the sheet is as it was
    assert after.split("### Address")[0] == before.split("### Address")[0]
    sig = "## Electronic signature"
    assert after[after.index(sig):] == before[before.index(sig):]


def test_prepare_goes_on_when_the_sheet_refresh_fails(
        context, job_folder, tmp_path, monkeypatch, caplog):
    def boom(folder, answers):
        raise RuntimeError("synthetic-refresh-detail")
    monkeypatch.setattr(apply_data, "refresh_answer_sections", boom)
    e = _enqueue(job_folder, "https://boards.greenhouse.io/acme/jobs/1")
    run = apply_run._JobRun(_runner(context, tmp_path), context, e)
    assert run._prepare() == "https://boards.greenhouse.io/acme/jobs/1"
    assert run.catalog.facts["work_authorized"].value == "Yes"
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "the apply.md answers were not refreshed (RuntimeError)" in logged
    assert "synthetic-refresh-detail" not in logged


def test_prepare_logs_a_plain_reason_when_the_sheet_lacks_a_refreshable_section(
        context, job_folder, tmp_path, monkeypatch, caplog):
    # fix round 1, item 2: say what is known ("the sheet's answer sections
    # were not refreshed"), not a guess at which heading is missing
    monkeypatch.setattr(apply_data, "refresh_answer_sections", lambda folder, answers: False)
    e = _enqueue(job_folder, "https://boards.greenhouse.io/acme/jobs/1")
    run = apply_run._JobRun(_runner(context, tmp_path), context, e)
    assert run._prepare() == "https://boards.greenhouse.io/acme/jobs/1"
    assert run.catalog.facts["work_authorized"].value == "Yes"
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "the sheet's answer sections were not refreshed" in logged
    assert "no Standard answers section" not in logged


def test_the_runner_reads_the_store_once_per_drain(
        context, fixture_url, job_folder, catalog_builder, tmp_path, monkeypatch):
    reads = []
    real = apply_answers.load
    monkeypatch.setattr(apply_answers, "load", lambda *a, **k: reads.append(1) or real(*a, **k))
    _enqueue(job_folder, fixture_url("ashby_steps.html"))
    _enqueue(job_folder, fixture_url("ashby_steps.html"), jid="43")
    outcomes = _runner(context, tmp_path, auto_apply_submit=False).drain(cap=2)
    assert len(outcomes) == 2
    assert reads == [1]


def _damaged_store():
    apply_answers.STORE_PATH.write_text("{not json", encoding="utf-8")
    with pytest.raises(apply_answers.AnswerStoreError) as info:
        apply_answers.load()
    return info.value


def test_a_drain_over_a_damaged_store_claims_nothing(context, fixture_url, job_folder,
                                                     tmp_path):
    # FL-4: the store is read before the first claim
    _enqueue(job_folder, fixture_url("ashby_steps.html"))
    _damaged_store()
    with pytest.raises(apply_answers.AnswerStoreError):
        _runner(context, tmp_path).drain(cap=1)
    entry = _entry()
    assert entry["status"] == "queued" and int(entry.get("attempts") or 0) == 0


@pytest.mark.parametrize("verb", [["drain"], ["one", "42"]])
def test_main_exits_2_on_a_damaged_store_and_claims_nothing(
        hermetic_cli, monkeypatch, capsys, job_folder, verb):
    monkeypatch.setattr(apply_run.jev, "get", lambda mode="": jev_harness.judge())
    _enqueue(job_folder, "https://boards.greenhouse.io/acme/jobs/1")
    err = _damaged_store()
    assert apply_run.main([*verb, "--jev", "typesafe"]) == 2
    assert capsys.readouterr().err.strip() == (
        f"The Apply Answers file is damaged ({err.path}): {err.reason}. Open the "
        f"dashboard's Apply Answers tab to restore the backup.")
    entry = _entry()
    assert entry["status"] == "queued" and int(entry.get("attempts") or 0) == 0


_HEADER = ('Answers not confirmed: "Are you willing to relocate?", '
           '"Are you willing to work on-site (in the office)?", '
           '"Gender (EEO self-identification).". Questions that need them will stop.')


def test_a_drain_names_the_answers_not_confirmed_once_on_the_console_and_in_the_report(
        context, fixture_url, job_folder, catalog_builder, tmp_path, capsys):
    # FL-4: two built-ins not confirmed and one missing; their questions only
    bank = [e for e in unconfirmed(standard_bank(), "willing_to_relocate", "onsite_ok")
            if e["id"] != "gender"]
    apply_answers.save(bank)
    _enqueue(job_folder, fixture_url("ashby_steps.html"))
    runner = _runner(context, tmp_path, auto_apply_submit=False)
    outcomes = runner.drain(cap=1)
    assert len(outcomes) == 1
    out = capsys.readouterr().out
    assert out.count(_HEADER) == 1, out
    report = next(tmp_path.glob(f"{apply_run.DRAIN_REPORT_PREFIX}*.md"))
    text = report.read_text(encoding="utf-8")
    assert text.startswith("# Apply drain ") and text.count(_HEADER) == 1
    assert text.index(_HEADER) < text.index(apply_run.summary_line(outcomes))


def test_a_store_with_every_built_in_confirmed_has_no_header(context, tmp_path):
    runner = _runner(context, tmp_path)
    assert runner.load_answers() == apply_answers.load()
    assert runner.answers_header == ""


def test_write_drain_report_puts_the_header_under_the_title(tmp_path):
    from datetime import datetime
    out = apply_run.Outcome("1", "submitted", "confirmation page", "", 3, {})
    path = apply_run.write_drain_report([out], tmp_path / "q.json",
                                        now=datetime(2026, 9, 26, 12, 0, 0), header=_HEADER)
    text = path.read_text(encoding="utf-8")
    assert text.startswith(f"# Apply drain 20260926-120000\n\n{_HEADER}\n\n"
                           f"{apply_run.summary_line([out])}\n\n")
    plain = apply_run.write_drain_report([out], tmp_path / "q.json",
                                         now=datetime(2026, 9, 26, 12, 0, 0))
    assert plain.read_text(encoding="utf-8").startswith(
        f"# Apply drain 20260926-120000\n\n{apply_run.summary_line([out])}\n\n")


def test_default_profile_dir_sits_under_localappdata(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert apply_run.default_profile_dir() == tmp_path / "linkedin_watcher" / "browser_profile"


# --- the code box is the one the email is about; a postal code box is passed over ---------

def test_the_emailed_code_never_lands_in_a_postal_code_box(
        context, fixture_url, job_folder, catalog_builder, tmp_path):
    class Inbox:
        def fetch_code(self, page, site, inbox_url):
            return "MKPZ3QRA"

    _enqueue(job_folder, fixture_url("code_gate_postal.html"))
    runner = _runner(context, tmp_path)
    runner.inbox = Inbox()
    out = runner.drain(cap=1)[0]
    assert out.status == "submitted", out       # the gate accepted the code and moved on
    record = Path(out.record_path).read_text(encoding="utf-8")
    assert "Security code: <hidden>" in record
    assert "Postal code" not in record


@pytest.mark.parametrize("labels,expected", [
    (["Postal code", "Security code"], "Security code"),
    (["Zip code", "Verification code"], "Verification code"),
    (["Promo code", "One-time code"], "One-time code"),
    (["Country code", "OTP"], "OTP"),
    (["Access code"], "Access code"),                   # nothing preferred: the code word wins
    # a box that carries a code word without "code" sits above the real one
    (["Social Security Number", "Security code"], "Security code"),
    (["Work authorization status", "Verification code"], "Verification code"),
    (["Social Security Number", "Work authorization status", "Enter the code"],
     "Enter the code"),
    # more boxes that read as "code" and are no place for the emailed one
    (["Referral code", "Enter the code"], "Enter the code"),
    (["Invite code", "Access code"], "Access code"),
    (["Postcode", "Access code"], "Access code"),
])
def test_code_field_pick_prefers_the_verification_box(labels, expected):
    import apply_form
    fields = [apply_form.Field(n=i, locator=(0, f"#f{i}"), label=label, type="text",
                               required=False, id_or_name=f"f{i}")
              for i, label in enumerate(labels)]
    picked = apply_run._code_field(fields)
    assert picked is not None and picked.label == expected


# --- cycle 18 FM-1: the read-back check takes the judge's qualified pick as before -------------

def test_the_read_back_check_takes_a_qualified_pick_and_no_yes_inside_one():
    assert apply_run.pick_holds("No, I do not require sponsorship",
                                "No, I do not require sponsorship")
    assert apply_run.pick_holds("U.S. citizen or permanent resident",
                                "U.S. citizen or permanent resident")
    assert not apply_run.pick_holds("Yes - on a work visa (OPT/H-1B)", "Yes")
    assert not apply_run.pick_holds("Yes", "Yes, I am willing to relocate")
    assert apply_run.pick_holds("Y", "Yes")


# --- cycle 18: the Ashby-style replica, answered from the typed store -----------------------

def test_the_ashby_relocation_replica_parks_on_a_job_location_the_run_cannot_check(
        _browser, flow_server, tmp_path):
    # the Contoso incident's questions: legal authorization among qualified
    # options only, where a stored Yes is none of "Yes - on a work visa
    # (OPT/H-1B)", sponsorship on Yes / No buttons (settled in code by the
    # alias set) and relocation among combined options. SP6c round 5: the
    # plan cannot reach the job's location, so "the job location (New York)"
    # is another question than the stored relocation answer; the run parks
    # on it by the user's policy, before it fills or sends anything
    f = h.flow("ashby_relocation")
    assert (f.submit, f.status) == (True, "needs_human")
    r = h.run_flow(f, jev.FakeJev(), "fake", browser=_browser, server=flow_server,
                   workdir=tmp_path)
    assert r.ok and not r.breaks, r
    assert r.reason == ("required field without an answer: Are you willing to relocate to "
                        "the job location (New York)?")
    pages = [json.loads(p.read_text(encoding="utf-8"))
             for p in sorted(Path(r.trace).glob("page-*.json"),
                             key=lambda p: int(p.stem.split("-")[1]))]
    events = [e for p in pages for e in p["events"]]
    planned = {pf["label"]: (pf["fact_key"], pf["action"])
               for e in events if e["kind"] == "plan" for pf in e["plan"]["fields"]}
    assert planned["Are you willing to relocate to the job location (New York)?"] == (
        None, "skip")
    assert planned["Will you now or in the future require visa sponsorship?"] == (
        "requires_sponsorship", "select")
    assert not [e for e in events if e["kind"] == "verify"]


# --- cycle 18 FM-6: a draft is reused only for the same question in the same place -----------

def test_a_draft_key_names_the_label_the_help_and_the_section():
    import dataclasses

    import apply_form
    f = apply_form.Field(0, (0, "#q"), "Why this role?", "textarea", True,
                         help="Max 500 characters.", section="About you")
    key = apply_run._draft_key(f)
    assert key is not None
    assert apply_run._draft_key(dataclasses.replace(f, label="why this  ROLE?")) == key
    assert apply_run._draft_key(dataclasses.replace(f, section="Previous employer")) != key
    assert apply_run._draft_key(dataclasses.replace(f, help="Max 100 characters.")) != key


@pytest.mark.parametrize("label", [
    "please explain", "If yes, please explain", "If so, please explain:", "Explain",
    "Details", "Please specify", "Other", "Comments", "Additional information",
    "If other, please specify", "Please explain *", "DETAILS"])
def test_a_generic_label_never_keys_a_draft(label):
    import apply_form
    f = apply_form.Field(0, (0, "#q"), label, "textarea", True)
    assert apply_run._draft_key(f) is None


def test_a_generic_follow_up_gets_a_draft_of_its_own_each_time(tmp_path):
    from unittest.mock import Mock

    import apply_form

    class Gen:
        calls = 0
        last = None

        def answer(self, field, catalog, judge, *, budget):
            Gen.calls += 1
            return f"Draft {Gen.calls}."
    runner = apply_run.Runner(jev=jev.FakeJev(), context=Mock(), run_context={},
                              sleep=lambda s: None, answergen=Gen())
    run = apply_run._JobRun(runner, Mock(), {"job_posting_id": "s",
                                             "apply_url": "https://x.example/1"})
    run.catalog = apply_facts.build(h.write_job_folder(tmp_path / "job"), answers=h.bank())
    run.gen_budget = 4          # one draft for each of the four questions
    fields = [apply_form.Field(0, (0, "#a"), "If yes, please explain", "textarea", True,
                               section="Relocation"),
              apply_form.Field(1, (0, "#b"), "If yes, please explain", "textarea", True,
                               section="Criminal history"),
              apply_form.Field(2, (0, "#c"), "Why this role?", "textarea", True,
                               section="About you"),
              apply_form.Field(3, (0, "#d"), "Why this role?", "textarea", True,
                               section="Previous employer")]
    digest = apply_form.FormDigest("x.example", "Apply", "", fields=fields)
    plan = FillPlan(fields=[PlannedField(n=f.n, locator=f.locator, label=f.label,
                                         required=True, fact_key="needs_generation",
                                         value="", option=None, confidence=0.9,
                                         action="generate") for f in fields])
    run._resolve_generation(digest, plan, {"generated": []})
    assert Gen.calls == 4
    assert [pf.value for pf in plan.fields] == ["Draft 1.", "Draft 2.", "Draft 3.",
                                                "Draft 4."]


# --- cycle 18 FM-2: a list whose options could not be read ------------------------------

_UNREAD_AUTH = """<body><form>
  <span id="auth-label">Work authorization</span>
  <div id="auth" role="combobox" aria-labelledby="auth-label" aria-expanded="false"
       aria-haspopup="listbox" aria-controls="auth-menu" tabindex="0">
    <input id="auth-input" type="text" autocomplete="off" aria-labelledby="auth-label"></div>
  <ul id="auth-menu" role="listbox" hidden>
    <li role="option">Yes - on a work visa (OPT/H-1B)</li>
    <li role="option">U.S. citizen or permanent resident</li></ul>
  <script>
    const box = document.getElementById('auth'), menu = document.getElementById('auth-menu');
    const input = document.getElementById('auth-input');
    box.addEventListener('click', () => { menu.hidden = false;
                                          box.setAttribute('aria-expanded', 'true'); });
    menu.querySelectorAll('li').forEach((li) => li.onclick = () => {
      input.value = li.textContent; menu.hidden = true;
      box.setAttribute('aria-expanded', 'false'); });
  </script></form></body>"""


def _decisions(monkeypatch, run) -> list[tuple[str, dict]]:
    seen: list[tuple[str, dict]] = []
    real = run._decide

    def _decide(what, why, **evidence):
        seen.append((what, evidence))
        real(what, why, **evidence)
    monkeypatch.setattr(run, "_decide", _decide)
    return seen


@pytest.mark.parametrize("required", [True, False])
def test_a_list_whose_options_could_not_be_read_parks_or_stays_blank(
        context, job_folder, catalog_builder, tmp_path, monkeypatch, required):
    run = _unit_run(context, tmp_path, job_folder, _UNREAD_AUTH)
    seen = _decisions(monkeypatch, run)
    digest = apply_form.extract(run.page)
    auth = next(f for f in digest.fields if f.label.startswith("Work authorization"))
    plan = FillPlan(fields=[PlannedField(n=auth.n, locator=auth.locator,
                                         label="Work authorization", required=required,
                                         fact_key="work_authorized", value="Yes", option=None,
                                         confidence=0.95, action="fill")])
    if required:
        with pytest.raises(apply_run._Parked) as parked:
            run._fill_and_verify(digest, plan, run.pages[-1])
        assert (parked.value.status, parked.value.reason) == (
            "needs_human", "required field without an answer: Work authorization (its "
                           "options could not be read)")
    else:
        assert run._fill_and_verify(digest, plan, run.pages[-1]) == []
    assert run.page.locator("#auth-input").input_value() == ""
    assert [m["question"] for m in run.missing] == ["Work authorization"]
    assert [ev["fields"] for what, ev in seen if what == "options_unread"] == [
        ["Work authorization"]]


# a list whose menu never opens: neither a click nor typing shows an option
_NEVER_OPENS = """<body><form>
  <span id="auth-label">Work authorization</span>
  <div id="auth" role="combobox" aria-labelledby="auth-label" aria-expanded="false"
       aria-haspopup="listbox" aria-controls="auth-menu" tabindex="0">
    <input id="auth-input" type="text" autocomplete="off" aria-labelledby="auth-label"></div>
  <ul id="auth-menu" role="listbox" hidden></ul></form></body>"""

# a typeahead that offers only qualified answers for whatever is typed (the
# Contoso pattern)
_QUALIFIED_TYPEAHEAD = """<body><form>
  <label for="spon">Will you now or in the future require sponsorship?</label>
  <input id="spon" autocomplete="off"><ul id="spon-list" class="dropdown-results"></ul>
  <script>
    const box = document.getElementById('spon'), list = document.getElementById('spon-list');
    box.addEventListener('input', () => { list.innerHTML = '';
      if (!box.value) return;
      ['Yes, I will require sponsorship', 'No, I do not require sponsorship'].forEach((c) => {
        const li = document.createElement('li'); li.textContent = c;
        li.onclick = () => { box.value = c; }; list.appendChild(li); }); });
  </script></form></body>"""


def _typed_yes_no_parks_or_stays_blank(run, seen, digest, field, box, value, required,
                                       label):
    """Fill `value` into `field` with no options read ahead, and check the
    yes / no left no typed guess: a required field parks on it, an optional
    one comes back unverified and blank; either way it is an open question."""
    plan = FillPlan(fields=[PlannedField(n=field.n, locator=field.locator, label=label,
                                         required=required, fact_key="work_authorized",
                                         value=value, option=None, confidence=0.95,
                                         action="fill", widget=field.widget)])
    if required:
        with pytest.raises(apply_run._Parked) as parked:
            run._fill_and_verify(digest, plan, run.pages[-1])
        assert (parked.value.status, parked.value.reason) == (
            "needs_human", f"required field without an answer: {label} (its options could "
                           "not be read)")
    else:
        assert run._fill_and_verify(digest, plan, run.pages[-1]) == []
    assert run.page.locator(box).input_value() == ""
    assert [m["question"] for m in run.missing] == [label]
    assert [ev["fields"] for what, ev in seen if what == "options_unread"] == [[label]]


@pytest.mark.parametrize("required", [True, False])
def test_a_yes_or_no_typed_into_a_list_that_never_opens_is_taken_out(
        context, job_folder, catalog_builder, tmp_path, monkeypatch, required):
    # SP3 fix round 1 (FM-2): the list showed nothing, so the "Yes" typed to
    # bring its options stayed in the box and read back as a verified answer
    run = _unit_run(context, tmp_path, job_folder, _NEVER_OPENS)
    seen = _decisions(monkeypatch, run)
    digest = apply_form.extract(run.page)
    auth = next(f for f in digest.fields if f.label.startswith("Work authorization"))
    _typed_yes_no_parks_or_stays_blank(run, seen, digest, auth, "#auth-input", "Yes", required,
                                       "Work authorization")


@pytest.mark.parametrize("required", [True, False])
def test_a_yes_or_no_typed_into_a_typeahead_of_qualified_answers_is_taken_out(
        context, job_folder, catalog_builder, tmp_path, monkeypatch, required):
    # SP3 fix round 1 (FM-2): the typeahead offered only "Yes, I will ..." and
    # "No, I do not ...", and the typed "No" stayed as the answer
    run = _unit_run(context, tmp_path, job_folder, _QUALIFIED_TYPEAHEAD)
    seen = _decisions(monkeypatch, run)
    digest = apply_form.extract(run.page)
    spon = next(f for f in digest.fields if f.label.startswith("Will you now"))
    assert spon.widget == "typeahead"
    _typed_yes_no_parks_or_stays_blank(run, seen, digest, spon, "#spon", "No", required,
                                       "Sponsorship")


# --- cycle 18 FM-4: an optional answer that failed its check is taken out -------------------

class _FailsEveryCheck:
    """A judge that reads every typed value as wrong."""

    def judge(self, state, questions):
        return {qid: jev.Answer(kind="noul", noul=0.1) for qid in questions}


_OPTIONAL_BOXES = """<body><form>
  <label>Nickname <input id="nick"></label>
  <fieldset><legend>Preferred shift</legend>
    <label><input type="radio" name="shift" value="day"> Day</label>
    <label><input type="radio" name="shift" value="night"
      onchange="document.querySelector('[value=day]').checked = true"> Night</label>
  </fieldset></form></body>"""


def test_an_optional_answer_that_failed_its_check_is_cleared(
        context, job_folder, catalog_builder, tmp_path, monkeypatch):
    run = _unit_run(context, tmp_path, job_folder, _OPTIONAL_BOXES)
    run.r.jev = _FailsEveryCheck()
    seen = _decisions(monkeypatch, run)
    digest = apply_form.extract(run.page)
    nick = next(f for f in digest.fields if f.label.startswith("Nickname"))
    plan = FillPlan(fields=[PlannedField(n=nick.n, locator=nick.locator, label="Nickname",
                                         required=False, fact_key="first_name", value="JD",
                                         option=None, confidence=0.95, action="fill")])
    results = run._fill_and_verify(digest, plan, run.pages[-1])
    assert [(v.label, v.ok) for v in results] == [("Nickname", False)]
    assert run.page.locator("#nick").input_value() == ""
    assert run.pages[-1]["cleared"] == ["Nickname"]
    assert nick.n not in run._last_filled
    assert [ev["fields"] for what, ev in seen if what == "cleared_optional"] == [["Nickname"]]


def test_an_optional_answer_that_failed_its_check_and_cannot_be_cleared_parks(
        context, job_folder, catalog_builder, tmp_path, monkeypatch):
    run = _unit_run(context, tmp_path, job_folder, _OPTIONAL_BOXES)
    digest = apply_form.extract(run.page)
    shift = next(f for f in digest.fields if f.type == "radio")
    plan = FillPlan(fields=[PlannedField(n=shift.n, locator=shift.locator,
                                         label="Preferred shift", required=False,
                                         fact_key="answer_shift", value="Night",
                                         option="Night", confidence=0.95, action="select")])
    with pytest.raises(apply_run._Parked) as parked:
        run._fill_and_verify(digest, plan, run.pages[-1])
    assert (parked.value.status, parked.value.reason) == (
        "needs_human", "a wrong answer could not be removed: Preferred shift")
    assert h.policy_park(parked.value.status, parked.value.reason) is True
