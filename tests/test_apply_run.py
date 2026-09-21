"""`apply_run`: the drain loop over the local HTML fixtures, `FakeJev` and a
temp queue (headless Chromium through the module-scoped test browser; the
runner gets the test context injected so nothing holds a window). The
synthetic apply.md comes from `apply_data.build_markdown` over a synthetic
master, as `test_apply_facts.py` does; no real store, profile or site is
touched. Skips without Playwright or Chromium. No `asyncio.run()` here (see
`conftest_browser`)."""
import sys
from pathlib import Path

import pytest

pytest.importorskip("playwright")

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import apply_facts  # noqa: E402
import apply_judge  # noqa: E402
import apply_queue  # noqa: E402
import apply_run  # noqa: E402
import jev  # noqa: E402
from apply_judge import FillPlan, PlannedField, VerifyResult  # noqa: E402
from resume_tailor import apply_answers, apply_config, apply_data  # noqa: E402

pytest_plugins = ["conftest_browser"]

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
    bank = apply_answers.seed_defaults()
    values = {"work_authorized": "true", "requires_sponsorship": "false",
              "years_experience": "2", "willing_to_relocate": "true",
              "gender": "Decline to self-identify",
              "race_ethnicity": "Decline to self-identify",
              "veteran_status": "I am not a veteran",
              "disability_status": "No, I do not have a disability",
              "how_did_you_hear": "LinkedIn",
              "address_street": "123 Main Street", "address_city": "Anytown",
              "address_state": "California", "address_zip": "12345",
              "address_country": "United States"}
    for e in bank:
        if e["id"] in values:
            e["answer"] = values[e["id"]]
    return bank


@pytest.fixture(autouse=True)
def _hermetic(tmp_path, monkeypatch):
    monkeypatch.setattr(apply_config, "APPLY_CONFIG", tmp_path / "missing.json")
    monkeypatch.setattr(apply_answers, "STORE_PATH", tmp_path / "apply_answers.json")
    monkeypatch.setenv("APPLY_QUEUE_PATH", str(tmp_path / "apply_queue.json"))


@pytest.fixture
def catalog_builder(monkeypatch):
    """`apply_facts.build` with the synthetic bank, so the runner never opens
    the answer store."""
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


def _enqueue(job_folder, url, jid="42", **kw):
    e = apply_queue.new_entry(jid, company="Fabrikam", title="Analytics Engineer",
                              apply_url=url, **kw)
    apply_queue.enqueue(e)
    apply_queue.set_artifacts(jid, {"folder": str(job_folder),
                                    "apply_md": str(job_folder / "apply.md"),
                                    "resume_pdf": str(job_folder / "Jane_Doe_Resume.pdf")})
    return apply_queue.load()["jobs"][-1]


def _runner(context, tmp_path, **settings):
    base = {"auto_apply_submit": True, "auto_apply_headless": True,
            "auto_apply_jev_mode": "fake", "auto_apply_batch_cap": 10,
            "auto_apply_generate": True}
    base.update(settings)
    return apply_run.Runner(jev=jev.FakeJev(), profile_dir=tmp_path / "profile",
                            settings=base, context=context, run_context=_RUN_CONTEXT)


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
    assert "Requests: 0" in record and "Input tokens: 0" in record
    assert jev.MODEL in record
    assert out.pages >= 4
    assert out.jev_usage["requests"] == 0
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


def test_submit_click_is_never_retried(context, fixture_url, job_folder, catalog_builder,
                                       tmp_path, monkeypatch):
    monkeypatch.setattr(apply_run, "CLICK_TIMEOUT_S", 1)
    clicks = []
    real = apply_run.apply_fill.click_button

    def _counting(page, digest, n, **kw):
        got = real(page, digest, n, **kw)
        clicks.append((next(b.text for b in digest.buttons if b.n == n), got,
                       page.evaluate("window.__clicks")))
        return got
    monkeypatch.setattr(apply_run.apply_fill, "click_button", _counting)
    _enqueue(job_folder, fixture_url("slow_submit.html"))
    out = _runner(context, tmp_path).drain(cap=1)[0]
    assert clicks == [("Submit", False, 1)]           # one click, read as quiet, never repeated
    assert out.status == "submitted" and out.reason == "confirmation page"


# --- (c) the captcha page parks needs_human with the flag in the notes ----------------

def test_captcha_page_parks_needs_human_with_the_flag(
        context, fixture_url, job_folder, catalog_builder, tmp_path):
    _enqueue(job_folder, fixture_url("captcha.html"))
    out = _runner(context, tmp_path).drain(cap=1)[0]
    assert out.status == "needs_human", out
    entry = _entry()
    assert "captcha" in entry["notes"].lower()
    assert "has_captcha p=0.90" in entry["notes"]
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


# --- (e) MAX_PAGES exhaustion parks -------------------------------------------------

def test_max_pages_exhaustion_parks(context, fixture_url, job_folder, catalog_builder,
                                    tmp_path, monkeypatch):
    monkeypatch.setattr(apply_judge, "MAX_PAGES", 1)
    _enqueue(job_folder, fixture_url("ashby_steps.html"))
    out = _runner(context, tmp_path).drain(cap=1)[0]
    assert out.status == "needs_human", out
    assert out.reason == "page budget exhausted (1 pages)"
    assert out.pages == 1
    page = next(p for p in context.pages if not p.is_closed())
    assert page.locator("body[data-submitted]").count() == 0
    assert page.locator("#first_name").input_value() == "Jane"      # step 1 was filled


def test_wall_clock_exhaustion_parks(context, fixture_url, job_folder, catalog_builder,
                                     tmp_path):
    ticks = iter([0.0, 0.0, apply_run.JOB_WALL_CLOCK_S + 1.0] + [10_000.0] * 50)
    _enqueue(job_folder, fixture_url("ashby_steps.html"))
    runner = apply_run.Runner(jev=jev.FakeJev(), profile_dir=tmp_path / "profile",
                              settings={"auto_apply_headless": True}, context=context,
                              run_context=_RUN_CONTEXT, clock=lambda: next(ticks))
    out = runner.drain(cap=1)[0]
    assert out.status == "needs_human" and out.reason == "time budget exhausted"


def test_wall_clock_passing_mid_fill_stops_the_fill_on_the_runner_clock(
        context, fixture_url, job_folder, catalog_builder, tmp_path):
    """The runner's clock reaches the deadline after the loop check, inside
    `apply_fill.apply`: nothing is typed, and the next loop check parks."""
    base = 1e9                     # far past time.monotonic(): only the injected clock can expire
    now = [base]

    def _clock():
        return now[0]
    _enqueue(job_folder, fixture_url("ashby_steps.html"))
    runner = apply_run.Runner(jev=jev.FakeJev(), profile_dir=tmp_path / "profile",
                              settings={"auto_apply_headless": True}, context=context,
                              run_context=_RUN_CONTEXT, clock=_clock)
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
    assert out.status == "needs_human" and out.reason == "time budget exhausted"
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
    assert out.reason == "login wall"
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
    out = _runner(context, tmp_path).drain(cap=1)[0]
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
    assert "State: code_gate" in record and "Security code: MKPZ3QRA" in record
    assert "Verify and continue (advance)" in record


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


def test_can_submit_flags():
    plan = _ok_plan()
    plan.flags["asks_for_prohibited"] = 0.5
    assert apply_run.can_submit(plan, _ok_verification(), _ON) == (
        False, "page asks for prohibited data (p=0.50)")
    plan = _ok_plan()
    plan.flags["has_captcha"] = 0.31
    assert apply_run.can_submit(plan, _ok_verification(), _ON) == (
        False, "captcha or bot check on the page (p=0.31)")


def test_can_submit_submit_button():
    plan = _ok_plan()
    del plan.buttons["submit"]
    assert apply_run.can_submit(plan, _ok_verification(), _ON) == (False, "no submit button")
    plan = _ok_plan()
    plan.buttons["submit"] = (3, 0.85)
    assert apply_run.can_submit(plan, _ok_verification(), _ON) == (
        False, "submit button confidence 0.85 below 0.90")


# --- the review page resolves generation before the gate ---------------------------------

def test_review_page_resolves_generation_before_the_gate(
        context, job_folder, tmp_path, catalog_builder):
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
        run._review_page(digest, plan, rec)
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
    with pytest.raises(apply_run._Parked) as info:
        run._review_page(digest, plan, rec)
    # resolved: the gate now fails on verification (nothing was typed on a review page)
    assert info.value.reason == "required field not verified: Your motivation for this role"


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


# --- the CLI ----------------------------------------------------------------------------------

def test_main_drain_exits_2_when_the_judge_is_unavailable(monkeypatch, capsys):
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


def test_main_one_exits_2_when_the_job_is_not_queued(monkeypatch, capsys):
    monkeypatch.setattr(apply_run.jev, "get", lambda mode="": jev.FakeJev())
    assert apply_run.main(["one", "nope", "--jev", "fake"]) == 2
    assert "not queued" in capsys.readouterr().err


def test_main_drain_prints_the_summary_and_exits_0(monkeypatch, capsys):
    monkeypatch.setattr(apply_run.jev, "get", lambda mode="": jev.FakeJev())
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
                           "--jev", "fake"]) == 0
    out = capsys.readouterr().out
    assert out.strip().endswith("drained 1: submitted 1, ready_to_submit 0, needs_human 0, "
                                "failed 0; Jev 4 requests, 2000 tokens, $0.0001")
    assert seen["cap"] == 2
    assert seen["settings"]["auto_apply_submit"] is False
    assert seen["settings"]["auto_apply_headless"] is True
    assert seen["settings"]["auto_apply_jev_mode"] == "fake"


def test_main_unexpected_error_exits_1(monkeypatch, capsys):
    monkeypatch.setattr(apply_run.jev, "get", lambda mode="": jev.FakeJev())

    class R:
        def __init__(self, **kw):
            pass

        def drain(self, cap):
            raise RuntimeError("boom")
    monkeypatch.setattr(apply_run, "Runner", R)
    assert apply_run.main(["drain", "--jev", "fake"]) == 1
    assert "RuntimeError: boom" in capsys.readouterr().err


def test_doctor_prints_one_line_per_row_and_the_profile(tmp_path, capsys, monkeypatch):
    import setup_check
    monkeypatch.setattr(setup_check, "module_found", lambda name: True)
    monkeypatch.setattr(setup_check, "chromium_installed", lambda: True)
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


def test_default_profile_dir_sits_under_localappdata(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert apply_run.default_profile_dir() == tmp_path / "linkedin_watcher" / "browser_profile"
