"""SP1: the per-job trace (`apply_trace`), the record that keeps its earlier
attempts, the per-job log, the park reasons that name their evidence, the
read-only `probe` verb, and a closed browser stopping the drain (RES-01).

Headless Chromium through the module-scoped test browser; the flows, the
synthetic sheet and bank, and the hermetic stores come from `apply_harness`;
the judge is `FakeJev` or a scripted subclass. No network but the local
fixture server and routed fake hosts."""
import io
import json
import logging
import re
import sys
from pathlib import Path

import pytest

pytest.importorskip("playwright")

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import apply_form  # noqa: E402
import apply_harness as h  # noqa: E402
import apply_queue  # noqa: E402
import apply_run  # noqa: E402
import apply_trace  # noqa: E402
import jev  # noqa: E402
from apply_judge import FillPlan, PlannedField  # noqa: E402

pytest_plugins = ["conftest_browser"]


@pytest.fixture(autouse=True)
def _hermetic(tmp_path):
    with h.hermetic(tmp_path), h.fast_timing():
        yield


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


def _enqueue(folder, url, jid="42", queue=None):
    apply_queue.enqueue(apply_queue.new_entry(jid, company="Fabrikam", title="Analytics Engineer",
                                              apply_url=url), path=queue)
    apply_queue.set_artifacts(jid, {"folder": str(folder), "apply_md": str(folder / "apply.md"),
                                    "resume_pdf": str(folder / "Jane_Doe_Resume.pdf")},
                              path=queue)


def _runner(context, tmp_path, judge=None, **settings):
    base = {"auto_apply_submit": True, "auto_apply_headless": True,
            "auto_apply_jev_mode": "fake", "auto_apply_batch_cap": 10,
            "auto_apply_generate": True}
    base.update(settings)
    return apply_run.Runner(jev=judge or jev.FakeJev(), profile_dir=tmp_path / "profile",
                            settings=base, context=context,
                            run_context={"signup_email": h.SIGNUP_EMAIL,
                                         "inbox_url": "https://mail.example.com/inbox"},
                            sleep=lambda s: None)


def _entry(jid="42"):
    return next(e for e in apply_queue.load()["jobs"] if e["job_posting_id"] == jid)


def _pages(trace_dir: Path) -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8"))
            for p in sorted(trace_dir.glob("page-*.json"),
                            key=lambda p: int(p.stem.split("-")[1]))]


# --- DIAG-01: the trace --------------------------------------------------------------------

def test_every_judged_page_leaves_its_json_and_screenshot_and_no_value(
        _browser, flow_server, tmp_path):
    r = h.run_flow(h.flow("ashby_wizard"), jev.FakeJev(), "fake", browser=_browser,
                   server=flow_server, workdir=tmp_path)
    assert r.ok and not r.breaks, r
    trace = Path(r.trace)
    assert trace.name == "attempt-1"
    pages = _pages(trace)
    assert len(pages) == r.pages >= 4
    first = pages[0]
    # the digest: every field with its type, required flag and label; the buttons
    labels = {f["label"]: f for f in first["digest"]["fields"]}
    assert labels["First name"]["required"] is True and labels["Email"]["type"] == "email"
    assert [b["text"] for b in first["digest"]["buttons"]] == ["Continue"]
    # the page state's full distribution and every answer with its confidence
    assert first["state"] == "application_form" and first["confidence"] == 1.0
    assert set(first["page_state"]["probabilities"]) >= {"application_form", "job_posting",
                                                         "confirmation", "other"}
    assert first["answers"]["button_0_role"]["choice"] == "advance"
    assert first["answers"]["button_0_role"]["confidence"] == 1.0
    assert "noul" in first["answers"]["has_captcha"]
    # the first load's settle is the page's first decision (SP2), then the
    # plan without the values it types, then what the page's step did
    assert first["events"][0]["kind"] == "decision" and first["events"][0]["what"] == "settled"
    steps = [e for e in first["events"] if e["kind"] != "decision"]
    assert steps[0]["kind"] == "plan"
    plan = steps[0]["plan"]
    assert {f["label"]: f["action"] for f in plan["fields"]}["First name"] == "fill"
    assert all("value" not in f for f in plan["fields"])
    fill = next(e for e in first["events"] if e["kind"] == "fill")
    assert {f["label"] for f in fill["fields"]} >= {"First name", "Last name", "Email"}
    click = next(e for e in first["events"] if e["kind"] == "click")
    assert (click["text"], click["role"], click["clicked"], click["changed"]) == (
        "Continue", "advance", True, True)
    assert click["confidence"] == 1.0
    assert first["timings"]["judge_s"] >= 0 and first["timings"]["extract_s"] >= 0
    # the submit and its confirmation
    gate = next(e for p in pages for e in p["events"] if e["kind"] == "gate")
    assert gate["ok"] is True
    assert pages[-1]["state"] == "confirmation"
    for p in pages:
        assert (trace / f"page-{p['n']}.jpg").read_bytes()[:2] == b"\xff\xd8"
    assert (trace / "end.jpg").exists()
    run = json.loads((trace / "run.json").read_text(encoding="utf-8"))
    assert (run["status"], run["reason"], run["attempt"]) == ("submitted", "confirmation page", 1)
    assert run["url_chain"][0].endswith("/forms/ashby_steps.html")
    # no value the fill typed is anywhere in the trace's JSON
    blob = "".join(p.read_text(encoding="utf-8") for p in trace.glob("*.json"))
    for value in ("Doering", "jane.doe@example.com", "555-555-0100"):
        assert value not in blob, value


def test_the_trace_follows_the_popup_and_records_the_linkedin_shortcut(
        _browser, flow_server, tmp_path):
    r = h.run_flow(h.flow("linkedin_posting"), jev.FakeJev(), "fake", browser=_browser,
                   server=flow_server, workdir=tmp_path)
    assert r.ok and not r.breaks, r
    pages = _pages(Path(r.trace))
    decision = next(e for e in pages[0]["events"]
                    if e["kind"] == "decision" and e["what"] == "linkedin_handler")
    assert decision["found"] == "offsite" and decision["view"]["offsite"] == ["Apply"]
    assert pages[0]["page_state"] is None           # the handler asked no judge
    entry = next(e for e in pages[0]["events"] if e["kind"] == "apply_entry")
    assert entry["text"] == "Apply" and entry["popup"] is True
    assert entry["destination"].endswith("/forms/ashby_steps.html")
    run = json.loads((Path(r.trace) / "run.json").read_text(encoding="utf-8"))
    assert run["url_chain"][0] == "https://www.linkedin.com/jobs/view/4438751519/"
    assert any(u.startswith("https://www.linkedin.com/safety/go/") for u in run["url_chain"])
    assert run["url_chain"][-1].endswith("/forms/ashby_steps.html")


def test_screenshots_mask_the_password_and_code_boxes(context, tmp_path):
    page = context.new_page()
    page.set_content("""<body style="margin:0">
      <input id="email" type="email" value="shown@example.com" style="width:400px;height:80px">
      <input id="pw" type="password" value="hunter2hunter2"
             style="position:absolute;left:0;top:200px;width:400px;height:80px">
      <input id="code" name="security_code" value="MKPZ3QRA"
             style="position:absolute;left:0;top:400px;width:400px;height:80px">
      </body>""")
    masks = apply_trace.mask_locators(page)
    assert sum(m.count() for m in masks) == 2              # the password and the code box
    trace = apply_trace.Trace(tmp_path, attempt=1, job_id="42")
    trace.start()
    try:
        assert trace.screenshot(page, "page-1") == "page-1.jpg"
    finally:
        trace.close()
    image = pytest.importorskip("PIL.Image")
    with image.open(tmp_path / "apply_trace" / "attempt-1" / "page-1.jpg") as img:
        rgb = img.convert("RGB")
        for y in (240, 440):                               # the centre of each masked box
            r, g, b = rgb.getpixel((200, y))
            assert r > 200 and g < 90 and b > 200, (y, (r, g, b))
        r, g, b = rgb.getpixel((200, 40))                  # the email box is not masked
        assert not (r > 200 and g < 90 and b > 200)


def test_a_trace_that_cannot_write_never_ends_the_job(context, flow_server, tmp_path,
                                                      monkeypatch):
    def _broken(*a, **kw):
        raise OSError("disk full")
    monkeypatch.setattr(apply_trace, "write_json", _broken)
    folder = h.write_job_folder(tmp_path / "job")
    _enqueue(folder, flow_server.url("captcha.html"))
    out = _runner(context, tmp_path).drain(cap=1)[0]
    assert out.status == "needs_human" and out.reason.startswith("captcha or bot check")


# --- DIAG-02: the job log --------------------------------------------------------------------

def test_the_job_log_holds_the_jobs_lines_and_only_them(context, flow_server, tmp_path,
                                                         caplog):
    folder = h.write_job_folder(tmp_path / "job")
    _enqueue(folder, flow_server.url("captcha.html"))
    with caplog.at_level(logging.INFO):
        _runner(context, tmp_path).drain(cap=1)
        logging.getLogger("apply_run").info("a line after the job")
    text = (folder / "apply_trace" / "attempt-1" / "job.log").read_text(encoding="utf-8")
    assert "job 42: start" in text and "job 42: needs_human" in text
    assert "a line after the job" not in text
    assert not any(isinstance(hd, logging.FileHandler)
                   for hd in logging.getLogger("apply_run").handlers)


# --- DIAG-04: the record keeps earlier attempts ---------------------------------------------------

def test_the_record_links_its_trace_and_keeps_every_earlier_attempt(
        context, flow_server, tmp_path):
    folder = h.write_job_folder(tmp_path / "job")
    (folder / "apply_record.md").write_text(
        "# Apply record: before the trace\n\n- Status: failed\n- Reason: an old run\n"
        "- Written: 2026-09-20T10:00:00\n", encoding="utf-8")
    _enqueue(folder, flow_server.url("captcha.html"))
    first = _runner(context, tmp_path).drain(cap=1)[0]
    apply_queue.requeue("42")
    second = _runner(context, tmp_path).drain(cap=1)[0]
    assert first.record_path == second.record_path == str(folder / "apply_record.md")
    text = (folder / "apply_record.md").read_text(encoding="utf-8")
    assert "- Attempt: 2" in text
    assert "- Trace: [apply_trace/attempt-2](apply_trace/attempt-2/)" in text
    assert "(apply_trace/attempt-2/page-1.json)" in text
    earlier = text.split("## Earlier attempts", 1)[1]
    assert "- Before the trace: failed: an old run (2026-09-20T10:00:00)" in earlier
    assert re.search(r"- Attempt 1: needs_human: captcha or bot check[^\n]*"
                     r"\[record\]\(apply_trace/attempt-1/apply_record\.md\)", earlier), earlier
    kept = (folder / "apply_trace" / "attempt-1" / "apply_record.md").read_text(encoding="utf-8")
    assert "- Attempt: 1" in kept and "## Earlier attempts" in kept
    assert _entry()["attempts"] == 2


def test_write_record_without_a_trace_keeps_the_record_it_replaces(tmp_path):
    entry = apply_queue.new_entry("7", company="Acme", title="Engineer",
                                  apply_url="https://jobs.example.com/7")
    apply_run.write_record(tmp_path, entry, "failed", "first", [], {}, "")
    text = apply_run.write_record(tmp_path, entry, "needs_human", "second", [], {},
                                  "").read_text(encoding="utf-8")
    assert "- Reason: second" in text
    assert re.search(r"- Before the trace: failed: first \(", text), text
    assert "- Reason: first" in next((tmp_path / "apply_trace").glob("earlier-*.md")).read_text(
        encoding="utf-8")


# --- DIAG-03: park reasons that carry their evidence --------------------------------------------

class _ReadAs(jev.FakeJev):
    """The fake, with every page read as `STATE` at `CONF` (the rest spread),
    its Nouls as `NOULS` says (`apply_harness.read_as`)."""
    STATE = ""
    CONF = 0.0
    NOULS = "coherent"

    def judge(self, state, questions):
        out = super().judge(state, questions)
        if "page_state" in out:
            probs = {self.STATE: self.CONF, "job_posting": 0.25, "application_form": 0.20}
            h.read_as(out, self.STATE, self.CONF, probs, nouls=self.NOULS)
        return out


# A page whose structure places it nowhere (no box, no Apply, no Next): the
# judge's read alone decides it (SP4: the structure reads a posting or a form
# itself, and an unsure read of one goes on as that)
_NOWHERE = ("<!doctype html><html><head><title>Life at Fabrikam</title></head><body>"
            "<h1>Life at Fabrikam</h1><p>" + "Our teams build analytics for retail partners "
            "across three continents, and we care about growth and learning. " * 3
            + "</p><button type='button'>Menu</button></body></html>")


def test_an_unsure_park_names_the_page_state_distribution(context, tmp_path):
    folder = h.write_job_folder(tmp_path / "job")
    _careers(context, {"/life": _NOWHERE})
    _enqueue(folder, "https://careers.fabrikam.example/life")
    judge = type("J", (_ReadAs,), {"STATE": "other", "CONF": 0.30, "NOULS": "neutral"})()
    out = _runner(context, tmp_path, judge).drain(cap=1)[0]
    assert out.reason == ("unsure what this page is (other, 0.30); reads: other 0.30, "
                          "job_posting 0.25, application_form 0.20"), out


def test_an_unrecognised_page_names_the_read(context, tmp_path):
    folder = h.write_job_folder(tmp_path / "job")
    _careers(context, {"/life": _NOWHERE})
    _enqueue(folder, "https://careers.fabrikam.example/life")
    judge = type("J", (_ReadAs,), {"STATE": "other", "CONF": 0.55, "NOULS": "neutral"})()
    out = _runner(context, tmp_path, judge).drain(cap=1)[0]
    assert out.reason == ("unrecognised page; reads: other 0.55, job_posting 0.25, "
                          "application_form 0.20"), out


_DEAD_CONTINUE = """<!doctype html><html><head><title>Apply</title></head><body>
<h1>Apply for Analytics Engineer</h1>
<label>First name * <input name="first" required></label>
<button type="button">Continue</button><button type="button">Help</button>
</body></html>"""


def test_an_advance_that_does_nothing_names_the_button_its_role_and_confidence(
        context, tmp_path):
    folder = h.write_job_folder(tmp_path / "job")
    context.route("https://careers.fabrikam.example/**",
                  lambda route: route.fulfill(body=_DEAD_CONTINUE, content_type="text/html"))
    _enqueue(folder, "https://careers.fabrikam.example/apply/42")
    out = _runner(context, tmp_path).drain(cap=1)[0]
    assert out.reason == ("the advance button (Continue) did nothing (judged advance 1.00, "
                          "clicked twice)"), out
    trace = folder / "apply_trace" / "attempt-1"
    clicks = [e for p in _pages(trace) for e in p["events"] if e["kind"] == "click"]
    assert [(c["text"], c["changed"], c.get("retry", False)) for c in clicks] == [
        ("Continue", False, False), ("Continue", False, True)]


def test_no_way_forward_names_the_buttons_it_saw(context, tmp_path):
    folder = h.write_job_folder(tmp_path / "job")
    html = _DEAD_CONTINUE.replace('<button type="button">Continue</button>', "")
    context.route("https://careers.fabrikam.example/**",
                  lambda route: route.fulfill(body=html, content_type="text/html"))
    _enqueue(folder, "https://careers.fabrikam.example/apply/42")
    out = _runner(context, tmp_path).drain(cap=1)[0]
    assert out.reason == "no way forward on this page (buttons: Help other 1.00)", out


# --- DIAG-05: swallowed errors become trace events ----------------------------------------------

def test_a_fill_that_raises_leaves_its_error_type_in_the_trace(context, tmp_path):
    folder = h.write_job_folder(tmp_path / "job")
    _enqueue(folder, "https://careers.fabrikam.example/jobs/42")
    run = apply_run._JobRun(_runner(context, tmp_path), context, _entry())
    run._prepare()
    run.trace = apply_trace.Trace(folder, attempt=1, job_id="42")
    run.trace.start()
    try:
        run.page = context.new_page()
        run.page.set_content('<body><label>Nickname <input id="nick"></label></body>')
        digest = apply_form.extract(run.page)
        run._new_page_record("application_form", 1.0, digest=digest, answers={})
        plan = FillPlan(fields=[PlannedField(n=0, locator=(0, "#gone"), label="Nickname",
                                             required=False, fact_key="first_name",
                                             value="Jane", option=None, confidence=1.0,
                                             action="fill")])
        run._fill_and_verify(digest, plan, run.pages[-1])
    finally:
        run.trace.close()
    fill = next(e for e in _pages(folder / "apply_trace" / "attempt-1")[0]["events"]
                if e["kind"] == "fill")
    assert fill["errors"] == [{"n": 0, "label": "Nickname", "action": "fill",
                               "error": "LookupError"}]
    assert "Jane" not in json.dumps(fill)


def test_an_account_step_error_is_traced_by_its_type(context, tmp_path, monkeypatch):
    folder = h.write_job_folder(tmp_path / "job")
    _enqueue(folder, "https://careers.fabrikam.example/jobs/42")
    run = apply_run._JobRun(_runner(context, tmp_path), context, _entry())
    run._prepare()
    run.trace = apply_trace.Trace(folder, attempt=1, job_id="42")
    run.trace.start()
    monkeypatch.setattr(apply_run.ats_accounts, "has_password", lambda: True)

    class _Links:
        def filter(self, **kw):
            return self

        def count(self):
            raise RuntimeError("typed secret-ish value")

    class _Page:
        url = "https://careers.fabrikam.example/login"

        def get_by_role(self, *a, **kw):
            return _Links()
    try:
        run.page = context.new_page()
        digest = apply_form.FormDigest(url_host="careers.fabrikam.example", title="Sign in",
                                       text="Sign in")
        run._new_page_record("login_wall", 1.0, digest=digest, answers={})
        assert run.accounts.login(_Page(), digest, "careers.fabrikam.example") is False
    finally:
        run.trace.close()
    events = _pages(folder / "apply_trace" / "attempt-1")[0]["events"]
    assert {"kind": "error", "step": "accounts.login", "error": "RuntimeError"}.items() <= \
        next(e for e in events if e["kind"] == "error").items()
    assert "secret-ish" not in json.dumps(events)


# --- the probe ---------------------------------------------------------------------------------

@pytest.fixture
def no_typing(monkeypatch):
    """Every Playwright call that would put a value on a page, recorded."""
    from playwright.sync_api import Frame, Locator
    calls = []
    for cls in (Locator, Frame):
        for name in ("fill", "type", "press_sequentially", "check", "uncheck", "set_checked",
                     "select_option", "set_input_files"):
            if hasattr(cls, name):
                monkeypatch.setattr(cls, name,
                                    lambda *a, _n=name, **kw: calls.append(_n))
    return calls


def test_probe_reads_a_posting_follows_only_its_apply_and_changes_nothing(
        context, flow_server, no_typing):
    out = io.StringIO()
    code = apply_run.probe(flow_server.url("job_posting.html"), follow_apply=True,
                           judge=jev.FakeJev(), context=context, out=out, settle_s=1)
    text = out.getvalue()
    assert code == 0, text
    assert "page 1: " + flow_server.url("job_posting.html") in text
    assert "  button [0] 'Apply now'" in text
    assert "  fieldless posting: would click [0] 'Apply now'" in text
    assert "  linkedin handler: not on LinkedIn" in text
    assert re.search(r"  judge: page_state job_posting 1\.00 \(job_posting 1\.00", text), text
    assert "follow-apply: clicked [0] 'Apply now'; a new tab at " in text
    assert "page 2: " + flow_server.url("ashby_steps.html") in text
    assert "  field [0] text required 'First name'" in text
    assert no_typing == []
    for page in context.pages:
        assert page.locator("body[data-submitted]").count() == 0
        assert page.locator("#first_name").count() == 0 or \
            page.locator("#first_name").input_value() == ""


def test_probe_takes_the_linkedin_shortcut_on_a_linkedin_job_page(
        context, flow_server, no_typing):
    for glob, body in h._linkedin_routes(flow_server.base).items():
        context.route(glob, h._fulfiller(body))
    out = io.StringIO()
    code = apply_run.probe("https://www.linkedin.com/jobs/view/4438751519/", context=context,
                           out=out, settle_s=1)
    text = out.getvalue()
    assert code == 0, text
    assert "  linkedin handler: click the offsite Apply 'Apply' (the LinkedIn handler); " \
           "the top card's offsite Apply 'Apply'" in text, text
    assert "judge:" not in text                              # no judge asked without --judge
    assert no_typing == []


def test_probe_never_clicks_on_a_page_with_form_fields(context, flow_server, no_typing):
    out = io.StringIO()
    code = apply_run.probe(flow_server.url("lever_single.html"), follow_apply=True,
                           context=context, out=out, settle_s=1)
    text = out.getvalue()
    assert code == 0, text
    assert re.search(r"follow-apply: nothing clicked \(the page has \d+ form field", text), text
    assert "page 2:" not in text
    assert no_typing == []
    assert context.pages[0].locator("body[data-submitted]").count() == 0


def test_main_probe_wires_its_flags(monkeypatch):
    seen = {}
    monkeypatch.setattr(apply_run, "_load_env", lambda: None)
    monkeypatch.setattr(apply_run, "load_settings", lambda: dict(apply_run.DEFAULT_SETTINGS))
    monkeypatch.setattr(apply_run.jev, "get", lambda mode="": ("judge", mode))
    monkeypatch.setattr(apply_run, "probe", lambda url, **kw: seen.update(url=url, **kw) or 0)
    assert apply_run.main(["probe", "https://jobs.example.com/1", "--follow-apply", "--judge",
                           "--jev", "fake", "--headed"]) == 0
    assert seen["url"] == "https://jobs.example.com/1"
    assert seen["follow_apply"] is True and seen["headed"] is True
    assert seen["judge"] == ("judge", "fake") and seen["profile_dir"] is None
    seen.clear()
    assert apply_run.main(["probe", "https://jobs.example.com/1"]) == 0
    assert seen["judge"] is None and seen["follow_apply"] is False


# --- RES-01: a closed browser stops the drain ------------------------------------------------------

class _ClosingJudge(jev.FakeJev):
    """The fake, closing `TARGET` (the context or the browser) on its first
    page read."""

    def __init__(self, target):
        self.target = target
        self.closed = False

    def judge(self, state, questions):
        if "page_state" in questions and not self.closed:
            self.closed = True
            self.target.close()
        return super().judge(state, questions)


def _three_jobs(tmp_path, url):
    for jid in ("a", "b", "c"):
        folder = h.write_job_folder(tmp_path / f"job-{jid}")
        _enqueue(folder, url, jid=jid)


@pytest.mark.parametrize("what", ["context", "browser"])
def test_a_closed_window_ends_the_job_and_stops_the_drain_with_the_rest_queued(
        _browser, flow_server, tmp_path, what):
    browser = _browser.browser_type.launch(headless=True) if what == "browser" else _browser
    ctx = browser.new_context()
    try:
        _three_jobs(tmp_path, flow_server.url("ashby_steps.html"))
        judge = _ClosingJudge(ctx if what == "context" else browser)
        outcomes = _runner(ctx, tmp_path, judge).drain(cap=10)
    finally:
        try:
            ctx.close()
        except Exception:       # noqa: BLE001  (already closed)
            pass
        if what == "browser":
            browser.close()
    assert [(o.job_id, o.status, o.reason) for o in outcomes] == [
        ("a", "needs_human", apply_run.CLOSED_REASON)]
    assert outcomes[0].browser_closed is True
    assert _entry("a")["status"] == "needs_human"
    for jid in ("b", "c"):
        e = _entry(jid)
        assert (e["status"], e["attempts"]) == ("queued", 0), e


def test_a_window_closed_between_jobs_claims_nothing_more(_browser, flow_server, tmp_path,
                                                          monkeypatch):
    ctx = _browser.new_context()
    real = apply_run._JobRun._finish

    def _finish_then_close(self, *a, **kw):
        out = real(self, *a, **kw)
        self.ctx.close()
        return out
    monkeypatch.setattr(apply_run._JobRun, "_finish", _finish_then_close)
    try:
        _three_jobs(tmp_path, flow_server.url("captcha.html"))
        outcomes = _runner(ctx, tmp_path).drain(cap=10)
    finally:
        try:
            ctx.close()
        except Exception:       # noqa: BLE001
            pass
    assert [o.job_id for o in outcomes] == ["a"]
    assert outcomes[0].reason.startswith("captcha or bot check")
    for jid in ("b", "c"):
        assert (_entry(jid)["status"], _entry(jid)["attempts"]) == ("queued", 0)


# === review round 1 ===============================================================================

# --- I1: a closed tab ends that job only ------------------------------------------------------------

_POPUP_POSTING = (REPO / "tests" / "fixtures" / "forms" / "job_posting.html").read_text(
    encoding="utf-8").replace('href="ashby_steps.html"',
                              'href="https://careers.fabrikam.example/apply/form"')
_SELF_CLOSING_FORM = """<!doctype html><html><head><title>Apply</title></head><body>
<h1>Apply for Analytics Engineer</h1>
<label>First name * <input name="first" required></label>
<button type="button" onclick="window.close()">Continue</button></body></html>"""


def _careers(context, pages: dict):
    """Serve `pages` (path -> html) on the application's fake host."""
    def _handle(route):
        path = "/" + route.request.url.split("careers.fabrikam.example/", 1)[1].split("?")[0]
        route.fulfill(body=pages.get(path, "<body>gone</body>"), content_type="text/html")
    context.route("https://careers.fabrikam.example/**", _handle)


def test_a_popup_that_closes_itself_ends_that_job_and_the_drain_goes_on(
        context, flow_server, tmp_path):
    _careers(context, {"/jobs/a": _POPUP_POSTING, "/apply/form": _SELF_CLOSING_FORM})
    _enqueue(h.write_job_folder(tmp_path / "job-a"), "https://careers.fabrikam.example/jobs/a",
             jid="a")
    _enqueue(h.write_job_folder(tmp_path / "job-b"), flow_server.url("captcha.html"), jid="b")
    outcomes = _runner(context, tmp_path).drain(cap=10)
    assert [(o.job_id, o.status) for o in outcomes] == [("a", "needs_human"),
                                                        ("b", "needs_human")], outcomes
    assert outcomes[0].reason.startswith(apply_run.TAB_CLOSED_REASON), outcomes[0]
    assert outcomes[0].browser_closed is False
    assert outcomes[1].reason.startswith("captcha or bot check")


class _TabClosingJudge(jev.FakeJev):
    """The fake, closing the job's own tab (the context's last page) on its
    first page read."""

    def __init__(self, context):
        self.context = context
        self.done = False

    def judge(self, state, questions):
        if "page_state" in questions and not self.done:
            self.done = True
            self.context.pages[-1].close()
        return super().judge(state, questions)


def test_a_closed_tab_with_the_window_open_ends_that_job_only(context, flow_server, tmp_path):
    _three_jobs(tmp_path, flow_server.url("captcha.html"))
    outcomes = _runner(context, tmp_path, _TabClosingJudge(context)).drain(cap=10)
    assert [o.job_id for o in outcomes] == ["a", "b", "c"], outcomes
    assert (outcomes[0].status, outcomes[0].browser_closed) == ("needs_human", False)
    assert outcomes[0].reason.startswith(apply_run.TAB_CLOSED_REASON), outcomes[0]
    assert all(o.reason.startswith("captcha or bot check") for o in outcomes[1:]), outcomes


# --- I2: a typed code is masked in every screenshot --------------------------------------------------

_CODE_BOX = ('<label for="verification-input">Verification code</label>'
             '<input id="verification-input" name="verification" '
             'style="position:absolute;left:0;top:0;width:400px;height:80px;font-size:40px">'
             '<button type="button" style="position:absolute;left:0;top:120px">Verify</button>')
_CODE_PAGE = ('<!doctype html><html><head><title>Verify your email</title></head>'
              '<body style="margin:0"><h1 style="margin:0 0 0 500px">Check your inbox</h1>'
              '<p style="margin-left:500px">Enter the code we emailed you.</p>'
              '<div style="position:absolute;left:0;top:300px">' + _CODE_BOX + '</div>'
              '</body></html>')
_CODE_FRAME_PAGE = ('<!doctype html><html><head><title>Verify your email</title></head>'
                    '<body style="margin:0"><h1 style="margin:0 0 0 500px">Check your inbox</h1>'
                    '<p style="margin-left:500px">Enter the code we emailed you.</p>'
                    '<iframe src="https://careers.fabrikam.example/codeframe" '
                    'style="position:absolute;left:0;top:300px;width:600px;height:300px;'
                    'border:0"></iframe></body></html>')
_CODE_FRAME = ('<!doctype html><html><body style="margin:0">'
               '<p style="position:absolute;left:450px">Security check</p>' + _CODE_BOX
               + '</body></html>')
_CODE = "MKPZ3QRA"


class _CodeGateJudge(jev.FakeJev):
    """The fake, reading any page with a verification box as the code gate."""

    def judge(self, state, questions):
        out = super().judge(state, questions)
        if "page_state" in out and any("Verification" in f.get("label", "")
                                       for f in state.get("fields") or []):
            out["page_state"] = jev.Answer(kind="choice", choice="code_gate",
                                           probabilities={"code_gate": 1.0}, confidence=1.0)
        return out


@pytest.mark.parametrize("framed", [False, True])
def test_a_typed_code_is_masked_in_the_screenshots_and_written_nowhere(
        context, tmp_path, caplog, framed):
    _careers(context, {"/verify": _CODE_FRAME_PAGE if framed else _CODE_PAGE,
                       "/codeframe": _CODE_FRAME})
    folder = h.write_job_folder(tmp_path / "job")
    _enqueue(folder, "https://careers.fabrikam.example/verify")
    runner = _runner(context, tmp_path, _CodeGateJudge())

    class _Inbox:
        def fetch_code(self, page, site, inbox_url):
            return _CODE
    runner.inbox = _Inbox()
    with caplog.at_level(logging.DEBUG):
        out = runner.drain(cap=1)[0]
    assert out.status == "needs_human", out
    page = next(p for p in context.pages if not p.is_closed())
    box = page.frames[1 if framed else 0].locator("#verification-input")
    assert box.input_value() == _CODE                     # the code is on the page
    trace = folder / "apply_trace" / "attempt-1"
    for path in [*trace.iterdir(), folder / "apply_record.md"]:
        if path.suffix != ".jpg":
            assert _CODE not in path.read_text(encoding="utf-8"), path.name
    assert _CODE not in caplog.text
    image = pytest.importorskip("PIL.Image")
    with image.open(trace / "end.jpg") as img:
        r, g, b = img.convert("RGB").getpixel((200, 340))     # the centre of the code box
        assert r > 200 and g < 90 and b > 200, (r, g, b)


def test_mask_locators_reach_into_frames(context, tmp_path):
    _careers(context, {"/verify": _CODE_FRAME_PAGE, "/codeframe": _CODE_FRAME.replace(
        'name="verification"', 'name="verification" type="password"')})
    page = context.new_page()
    page.goto("https://careers.fabrikam.example/verify")
    page.frames[1].wait_for_selector("#verification-input")
    assert sum(m.count() for m in apply_trace.mask_locators(page)) == 1


# --- every park reason names its evidence ------------------------------------------------------------

class _ReviewJudge(jev.FakeJev):
    def judge(self, state, questions):
        out = super().judge(state, questions)
        if "page_state" in out:
            out["page_state"] = jev.Answer(kind="choice", choice="review_page",
                                           probabilities={"review_page": 0.9}, confidence=0.9)
        return out


def test_no_submit_button_names_the_buttons_seen(context, tmp_path):
    _careers(context, {"/review": "<body><h1>Review your application</h1>"
                                  "<button type='button'>Back</button></body>"})
    _enqueue(h.write_job_folder(tmp_path / "job"), "https://careers.fabrikam.example/review")
    out = _runner(context, tmp_path, _ReviewJudge()).drain(cap=1)[0]
    assert out.reason == "no submit button (buttons: Back back 1.00)", out


@pytest.mark.parametrize("fixture, prefix", [("login_wall.html", "login wall"),
                                             ("signup.html", "account signup needed")])
def test_an_account_park_names_the_read_the_boxes_and_the_buttons(
        context, flow_server, tmp_path, fixture, prefix):
    _enqueue(h.write_job_folder(tmp_path / "job"), flow_server.url(fixture))
    out = _runner(context, tmp_path).drain(cap=1)[0]
    assert out.reason.startswith(f"{prefix} (read as "), out
    assert "master password stored: no" in out.reason
    assert "boxes: " in out.reason and "buttons: " in out.reason


def test_the_page_budget_names_the_last_pages(context, flow_server, tmp_path, monkeypatch):
    monkeypatch.setattr(apply_run.apply_judge, "MAX_PAGES", 2)
    _enqueue(h.write_job_folder(tmp_path / "job"), flow_server.url("ashby_steps.html"))
    out = _runner(context, tmp_path).drain(cap=1)[0]
    assert out.reason == ("page budget exhausted (2 pages; last: application_form, "
                          "application_form)"), out


def test_the_time_budget_names_the_minutes_and_the_last_pages(context, flow_server, tmp_path):
    ticks = iter([0.0, 0.0, 0.0, 0.0] + [apply_run.JOB_WALL_CLOCK_S + 1.0] * 400)
    _enqueue(h.write_job_folder(tmp_path / "job"), flow_server.url("ashby_steps.html"))
    runner = _runner(context, tmp_path)
    runner.clock = lambda: next(ticks)
    out = runner.drain(cap=1)[0]
    assert re.fullmatch(r"time budget exhausted \(15 min; \d+ page\(s\)(; last: [a-z_, ]+)?\)",
                        out.reason), out


def test_evidence_is_capped(context, tmp_path):
    buttons = "".join(f"<button type='button'>Button number {i} with a long label</button>"
                      for i in range(30))
    _careers(context, {"/review": f"<body><h1>Review your application</h1>{buttons}</body>"})
    _enqueue(h.write_job_folder(tmp_path / "job"), "https://careers.fabrikam.example/review")
    out = _runner(context, tmp_path, _ReviewJudge()).drain(cap=1)[0]
    assert out.reason.startswith("no submit button (buttons: ")
    assert len(out.reason) <= apply_run.EVIDENCE_CAP + 40, len(out.reason)


# --- the probe prints the loop's step; only a plain Apply is followed --------------------------------

@pytest.mark.parametrize("fixture, step", [
    ("job_posting.html", "click the Apply entry [0] 'Apply now' (judged_apply_entry)"),
    ("ashby_steps.html", "fill the page, then click the advance [0] 'Continue'"),
    ("lever_single.html", "fill the page, then the submit gate with [0] 'Submit application'")])
def test_probe_prints_the_step_the_loop_would_take(context, flow_server, fixture, step):
    out = io.StringIO()
    assert apply_run.probe(flow_server.url(fixture), judge=jev.FakeJev(), context=context,
                           out=out, settle_s=1) == 0
    assert f"  the loop would: {step}" in out.getvalue(), out.getvalue()


def test_probe_prints_an_unsure_park_and_a_remapped_sign_in(context, flow_server):
    out = io.StringIO()
    _careers(context, {"/life": _NOWHERE})
    judge = type("J", (_ReadAs,), {"STATE": "other", "CONF": 0.30, "NOULS": "neutral"})()
    apply_run.probe("https://careers.fabrikam.example/life", judge=judge, context=context,
                    out=out, settle_s=1)
    assert ("  the loop would: park: unsure what this page is (other, 0.30); reads: other "
            "0.30, job_posting 0.25, application_form 0.20") in out.getvalue(), out.getvalue()
    out = io.StringIO()
    judge = type("J", (_ReadAs,), {"STATE": "login_wall", "CONF": 0.90})()
    apply_run.probe(flow_server.url("lever_single.html"), judge=judge, context=context, out=out,
                    settle_s=1)
    assert ("  the loop would: read as login_wall with no account boxes: it is the form; "
            "fill the page, then the submit gate with [0] 'Submit application'"
            in out.getvalue()), out.getvalue()


@pytest.mark.parametrize("label", ["Quick apply", "Apply with LinkedIn", "1-click apply",
                                   "Easy Apply"])
def test_probe_follows_only_a_plain_apply(context, no_typing, label):
    _careers(context, {"/jobs/1": f"<body><h1>Engineer</h1><p>The role.</p>"
                                  f"<a class='btn' href='/apply'>{label}</a></body>"})
    out = io.StringIO()
    apply_run.probe("https://careers.fabrikam.example/jobs/1", follow_apply=True,
                    context=context, out=out, settle_s=1)
    assert "follow-apply: nothing clicked" in out.getvalue(), out.getvalue()
    assert "page 2:" not in out.getvalue()


# --- M3: a failed fill never logs its value ---------------------------------------------------------

def test_a_failed_fill_logs_its_error_type_and_never_its_value(context, monkeypatch, caplog):
    import apply_fill
    page = context.new_page()
    page.set_content('<body><label>Nickname <input id="nick"></label></body>')

    def _raise(*a, **kw):
        raise RuntimeError('Locator.fill: Timeout\nCall log:\n  - fill("SECRET-VALUE-42")')
    monkeypatch.setattr(apply_fill, "_act", _raise)
    plan = FillPlan(fields=[PlannedField(n=0, locator=(0, "#nick"), label="Nickname",
                                         required=False, fact_key="first_name",
                                         value="SECRET-VALUE-42", option=None, confidence=1.0,
                                         action="fill")])
    errors = []
    with caplog.at_level(logging.DEBUG):
        apply_fill.apply(page, plan, errors=errors)
    assert "SECRET-VALUE-42" not in caplog.text
    assert "RuntimeError" in caplog.text and "Nickname" in caplog.text
    assert errors == [{"n": 0, "label": "Nickname", "action": "fill", "error": "RuntimeError"}]


# --- M4: the record's links work where each copy sits ----------------------------------------------

_LINK = re.compile(r"\]\(([^)]+)\)")


def test_every_link_in_the_record_and_its_copies_resolves(context, flow_server, tmp_path):
    folder = h.write_job_folder(tmp_path / "job")
    (folder / "apply_record.md").write_text("# old\n\n- Status: failed\n- Reason: old\n",
                                            encoding="utf-8")
    _enqueue(folder, flow_server.url("captcha.html"))
    _runner(context, tmp_path).drain(cap=1)
    apply_queue.requeue("42")
    _runner(context, tmp_path).drain(cap=1)
    records = [folder / "apply_record.md",
               *sorted((folder / "apply_trace").glob("attempt-*/apply_record.md"))]
    assert len(records) == 3
    for record in records:
        links = _LINK.findall(record.read_text(encoding="utf-8"))
        assert links, record
        for link in links:
            assert (record.parent / link).exists(), (record, link)


# --- M8: an odd value in an event never turns the trace off -----------------------------------------

def test_an_odd_value_in_an_event_is_written_as_text(tmp_path):
    trace = apply_trace.Trace(tmp_path, attempt=1, job_id="42")
    trace.start()
    try:
        trace.event("odd", things={3, 1}, where=Path("a") / "b", obj=object())
        assert trace.enabled
        run = json.loads((tmp_path / "apply_trace" / "attempt-1" / "run.json").read_text(
            encoding="utf-8"))
        odd = run["setup_events"][-1]
        assert odd["kind"] == "odd" and isinstance(odd["things"], str)
        assert odd["where"] in ("a/b", "a\\b")
    finally:
        trace.close()


# --- M9: the sign-up page the login hook reads is a traced page ------------------------------------

def test_the_sign_up_page_the_login_hook_follows_is_traced(context, flow_server, tmp_path,
                                                          monkeypatch):
    monkeypatch.setattr(apply_run.ats_accounts, "_get_master_password", lambda: h.PASSWORD)
    folder = h.write_job_folder(tmp_path / "job")
    _enqueue(folder, flow_server.url("login_two_signup_links.html"))
    out = _runner(context, tmp_path, auto_apply_submit=False).drain(cap=1)[0]
    assert out.status == "ready_to_submit", out
    pages = _pages(folder / "apply_trace" / "attempt-1")
    assert [p["state"] for p in pages[:2]] == ["login_wall", "signup_form"]
    assert pages[1]["url"].endswith("/forms/signup.html")
    assert any(e["kind"] == "decision" and e["what"] == "signup_link"
               for e in pages[0]["events"])
