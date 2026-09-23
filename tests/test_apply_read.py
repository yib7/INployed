"""SP4: page reading that holds up.

- READ-06: a page read as a review with a confident advance and no submit
  clicks the advance (a wizard's middle step misread as the review), a review
  with its submit goes to the gate, a final-shaped advance on a review goes
  to the gate in either mode.

Headless Chromium through the module-scoped test browser; the fixtures are
served by the flow server, fake hosts are routed; the judge is `FakeJev`,
`NoisyJev` or a scripted subclass. No network."""
import json
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
import jev  # noqa: E402
from apply_judge import FillPlan  # noqa: E402

pytest_plugins = ["conftest_browser"]

CAREERS = "https://careers.fabrikam.example"


@pytest.fixture(autouse=True)
def _hermetic(tmp_path):
    with h.hermetic(tmp_path), h.fast_timing():
        yield


@pytest.fixture
def context(_browser):
    ctx = _browser.new_context()
    h.offline(ctx)          # the tests' own routes, added later, answer first
    try:
        yield ctx
    finally:
        ctx.close()


def _enqueue(folder, url, jid="42", **kw):
    apply_queue.enqueue(apply_queue.new_entry(jid, company="Fabrikam", title="Analytics Engineer",
                                              apply_url=url, **kw))
    apply_queue.set_artifacts(jid, {"folder": str(folder), "apply_md": str(folder / "apply.md"),
                                    "resume_pdf": str(folder / "Jane_Doe_Resume.pdf")})


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


def _drain(context, tmp_path, url, judge=None, **settings):
    """One job at `url` through the runner, with the harness's action
    recorder on: (the outcome, the recorder, the job folder)."""
    folder = h.write_job_folder(tmp_path / "job")
    _enqueue(folder, url)
    rec = h.Recorder(None, park_mode=not settings.get("auto_apply_submit", True))
    with rec.recording():
        out = _runner(context, tmp_path, judge, **settings).drain(cap=1)[0]
    return out, rec, folder


def _flow(name, _browser, flow_server, tmp_path, judge=None, judge_name="fake"):
    return h.run_flow(h.flow(name), judge or jev.FakeJev(), judge_name, browser=_browser,
                      server=flow_server, workdir=tmp_path)


def _pages(trace_dir) -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8"))
            for p in sorted(Path(trace_dir).glob("page-*.json"),
                            key=lambda p: int(p.stem.split("-")[1]))]


def _decisions(trace_dir, what):
    return [e for p in _pages(trace_dir) for e in p["events"]
            if e["kind"] == "decision" and e["what"] == what]


def _serve(context, pages: dict, host=CAREERS):
    """`pages` (path -> html) on `host`; any other path is a blank page."""
    def _handle(route):
        path = "/" + route.request.url.split(host + "/", 1)[1].split("?")[0]
        route.fulfill(body=pages.get(path, "<body>gone</body>"), content_type="text/html")
    context.route(f"{host}/**", _handle)


def _digest(buttons, fields=()):
    return apply_form.FormDigest(
        url_host="careers.fabrikam.example", title="Review", text="Review your details",
        fields=[apply_form.Field(n=i, locator=(0, f"#f{i}"), label=label, type="text",
                                 required=True) for i, label in enumerate(fields)],
        buttons=[apply_form.Button(i, (0, f"#b{i}"), text) for i, text in enumerate(buttons)])


# --- READ-06: a review read with only Next clicks the Next --------------------------------------

def test_a_wizard_step_read_as_a_review_clicks_its_next_and_reaches_the_gate(
        _browser, flow_server, tmp_path):
    r = _flow("review_with_next", _browser, flow_server, tmp_path)
    assert r.ok and not r.breaks, r
    pages = _pages(Path(r.trace))
    assert pages[0]["state"] == "review_page", [p["state"] for p in pages]
    clicked = [e for e in pages[0]["events"] if e["kind"] == "click"]
    assert [(c["text"], c["role"]) for c in clicked] == [("Next", "advance")], clicked


@pytest.mark.parametrize("buttons, roles, park, step", [
    (("Back", "Next"), {"back": (0, 1.0), "advance": (1, 0.9)}, False,
     "fill the page, then click the advance [1] 'Next'"),
    (("Edit", "Submit application"), {"other": (0, 1.0), "submit": (1, 0.9)}, False,
     "fill the page, then the submit gate with [1] 'Submit application'"),
    # a final word on a review is its last step in either mode
    (("Back", "Confirm"), {"back": (0, 1.0), "advance": (1, 0.9)}, False,
     "fill the page, then the submit gate with [1] 'Confirm'"),
    (("Back",), {"back": (0, 1.0)}, False, "fill the page, then park: no submit button")])
def test_the_probe_says_what_a_review_read_does(buttons, roles, park, step):
    plan = FillPlan(buttons=dict(roles))
    got = apply_run.loop_step(CAREERS + "/review", _digest(buttons), plan, "review_page", 0.9,
                              park_mode=park)
    assert got == step, got


def test_a_confirm_advance_on_a_review_goes_through_the_gate_in_submit_mode(context, tmp_path):
    page = ("<body><h1>Review your application</h1><p>Please review before you confirm.</p>"
            "<label>Email * <input type='email' name='email' required></label>"
            "<button type='button'>Back</button>"
            "<button type='button' onclick=\"document.body.dataset.submitted = 1\">Confirm"
            "</button></body>")

    class _Reads(jev.FakeJev):
        def judge(self, state, questions):
            out = super().judge(state, questions)
            if "page_state" in out:
                out["page_state"] = jev.Answer(kind="choice", choice="review_page",
                                               probabilities={"review_page": 0.9},
                                               confidence=0.9)
            for b in state.get("buttons") or []:
                qid = f"button_{b['n']}_role"
                if b.get("text") == "Confirm" and qid in out:
                    out[qid] = jev.Answer(kind="choice", choice="advance", confidence=0.9,
                                          probabilities={"advance": 0.9, "submit": 0.1})
            return out
    _serve(context, {"/review": page})
    out, rec, _ = _drain(context, tmp_path, f"{CAREERS}/review", _Reads())
    confirm = [a for a in rec.actions if a.kind == "click" and a.text == "Confirm"]
    assert confirm and all(a.in_gate for a in confirm), rec.actions
    assert out.status != "submitted" or "confirmation" in out.reason or "unconfirmed" in out.reason
