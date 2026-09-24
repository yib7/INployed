"""SP6: the way on.

- What covers a click put away (ADV-04); a step's Next over a feedback
  Submit (ADV-05); a stranger that took the advance gives way to the form's
  own Next; a sign-up beside a sign-in (ADV-08); sign-ins with another
  site never the way on (ADV-09); controls the run cannot read named and
  parked on (EXT-01).

Headless Chromium through the module-scoped test browser; no network, no
judge but `FakeJev` or a scripted one."""
import sys
from pathlib import Path

import pytest

pytest.importorskip("playwright")

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import apply_fill  # noqa: E402
import apply_form  # noqa: E402
import apply_harness as h  # noqa: E402
import apply_run  # noqa: E402
import jev  # noqa: E402
from apply_judge import FillPlan  # noqa: E402

pytest_plugins = ["conftest_browser"]


def _decisions(r) -> list[dict]:
    import json
    out = []
    for p in sorted(Path(r.trace).glob("*.json")):
        out += [e for e in json.loads(p.read_text(encoding="utf-8")).get("events", [])
                if e.get("kind") == "decision"]
    return out


# === what covers a click, put away (ADV-04) ===============================================================

@pytest.mark.parametrize("name, cleared", [
    ("chat_launcher", "close '×'"), ("cookie_banner", "consent 'Reject All'")])
def test_a_cover_over_the_way_on_is_put_away_and_the_click_made_once_more(
        _browser, flow_server, tmp_path, name, cleared):
    # the chat is closed by its own close, never started; the consent overlay
    # that came with the scroll is rejected, never accepted
    r = h.run_flow(h.flow(name), jev.FakeJev(), "fake", browser=_browser, server=flow_server,
                   workdir=tmp_path)
    assert r.ok and not r.breaks, (r.status, r.reason, r.breaks)
    covered = [d for d in _decisions(r) if d["what"] == "overlay_cleared"]
    assert len(covered) == 1 and cleared in covered[0]["why"], covered
    clicks = [a.text for a in r.actions if a.kind == "click"]
    assert "Start chat" not in clicks and "Accept All Cookies" not in clicks, clicks


def test_a_cover_with_nothing_to_close_is_never_clicked_through(browser_page):
    # a sticky promo with only a sign-up button over the button: the run
    # never clicks the promo's own control, and the click is refused
    browser_page.set_content("""<body><form>
      <button type="button" id="go" style="position: fixed; bottom: 20px; right: 20px"
        onclick="document.body.dataset.went = 1">Next</button></form>
      <div id="promo" style="position: fixed; inset: 0; background: rgba(0,0,0,.3)">
        <button type="button" onclick="document.body.dataset.signed = 1">Sign up for alerts</button>
      </div></body>""")
    digest = apply_form.extract(browser_page)
    n = next(b.n for b in digest.buttons if b.text == "Next")
    result = apply_fill.click(browser_page, digest, n, timeout_s=2)
    assert not result.clicked and "none" in result.overlay
    assert browser_page.evaluate("[document.body.dataset.went, document.body.dataset.signed]") == [
        None, None]


# === the way on: a Next beside another Submit, two forms, sign-ins elsewhere (ADV-05, 08, 09) ==========

@pytest.mark.parametrize("name", ["next_and_feedback_submit", "two_forms", "apply_with_linkedin"])
def test_the_way_on_is_the_steps_own(_browser, flow_server, tmp_path, name):
    # the feedback box's Submit is never clicked; the sign-in's boxes never
    # take the address or the password; LinkedIn is never left for
    r = h.run_flow(h.flow(name), jev.FakeJev(), "fake", browser=_browser, server=flow_server,
                   workdir=tmp_path)
    assert r.ok and not r.breaks, (r.status, r.reason, r.breaks)
    assert not any("linkedin.com" in a.url for a in r.actions)


def _route_digest(text: str, *buttons: str) -> apply_form.FormDigest:
    return apply_form.FormDigest("x.example", "Apply", text, buttons=[
        apply_form.Button(i, (0, f"#b{i}"), t) for i, t in enumerate(buttons)])


@pytest.mark.parametrize("text, apart, step", [
    ("Step 1 of 3", False, "advance"), ("Step 3 of 3", False, "gate"),
    ("", True, "advance"), ("", False, "gate"), ("Page 2 / 2", True, "gate")])
def test_a_next_beside_a_submit_goes_on_unless_the_page_is_the_last_step(text, apart, step):
    digest = _route_digest(text, "Next", "Submit")
    plan = FillPlan(buttons={"advance": (0, 0.9), "submit": (1, 0.9)})
    got, button, _ = apply_run.form_route(digest, plan, park_mode=True, submit_apart=apart)
    assert (got, button[0]) == (step, 0 if step == "advance" else 1)


def test_a_step_read_as_a_review_goes_on_beside_another_boxs_submit_too():
    # the matrix's next_and_feedback_submit seed 8: the step read as a review
    digest = _route_digest("", "Next", "Submit")
    plan = FillPlan(buttons={"advance": (0, 0.88), "submit": (1, 0.93)})
    assert apply_run.review_route(digest, plan, submit_apart=True)[:2] == ("advance", (0, 0.88))
    assert apply_run.review_route(digest, plan)[:2] == ("gate", (1, 0.93))


def test_a_stranger_that_took_the_advance_gives_way_to_the_forms_own_next():
    # the chat window's words, judged under noise: its close (in the site's
    # top bar) or its "Start chat" took the advance, the form's Next was
    # judged other, and the plan's own "other" is the chat's button
    buttons = [apply_form.Button(0, (0, "#next"), "Next", in_form=True),
               apply_form.Button(1, (0, "#x"), "×", chrome=True),
               apply_form.Button(2, (0, "#chat"), "Start chat")]
    digest = apply_form.FormDigest("x.example", "Apply", "", buttons=buttons)
    judged = {0: "other", 1: "advance", 2: "other"}
    closed = FillPlan(buttons={"advance": (1, 0.84), "other": (2, 0.98)})
    assert apply_run.form_route(digest, closed, park_mode=True, judged=judged)[:2] == (
        "advance", (0, 0.5))
    started = FillPlan(buttons={"advance": (2, 0.84), "other": (0, 0.9)})
    assert apply_run.form_route(digest, started, park_mode=True,
                                judged={0: "other", 1: "other", 2: "advance"})[:2] == (
        "advance", (0, 0.5))
    # a form's own advance stands, and a Next outside any form takes nothing
    own = FillPlan(buttons={"advance": (0, 0.9), "other": (2, 0.98)})
    assert apply_run.form_route(digest, own, park_mode=True, judged={0: "advance",
                                                                     2: "other"})[1] == (0, 0.9)
    loose = apply_form.FormDigest("x.example", "Apply", "", buttons=[
        apply_form.Button(0, (0, "#next"), "Next"), buttons[2]])
    assert apply_run.form_route(loose, FillPlan(buttons={"advance": (1, 0.84)}), park_mode=True,
                                judged={0: "other", 1: "advance"})[1] == (1, 0.84)


def test_a_sign_in_with_another_site_never_holds_the_way_on(tmp_path):
    import apply_facts
    import apply_judge
    catalog = apply_facts.build(h.write_job_folder(tmp_path / "job"), answers=h.bank())
    digest = _route_digest("", "Continue with LinkedIn", "Next", "Apply using Indeed", "Apply")
    answers = {f"button_{n}_role": jev.Answer(kind="choice", choice=role, confidence=0.95,
                                              probabilities={role: 0.95})
               for n, role in enumerate(("advance", "advance", "apply_entry", "apply_entry"))}
    plan = apply_judge.plan(digest, catalog, answers)
    assert plan.buttons["advance"][0] == 1 and plan.buttons["apply_entry"][0] == 3
    account = _route_digest("", "Continue with Google", "Sign in")
    assert apply_run.account_advance(account, FillPlan(buttons={"advance": (0, 0.95)}))[0] == 1


# === a control the run cannot read (EXT-01's rest) ======================================================

def test_a_closed_shadow_root_and_a_form_associated_element_are_named_unreadable(
        browser_page, fixture_url):
    browser_page.goto(fixture_url("closed_shadow_controls.html"))
    rows = [r for r in apply_form.control_scan(browser_page) if r["kind"] == "unreadable"]
    assert [(r["label"], r["why"], r["required"], r["empty"]) for r in rows] == [
        ("Earliest start date", "closed shadow root", True, True),
        ("Open to relocation", "form-associated custom element", False, False)]
    # neither is a field the extractor reads
    assert [f.label for f in apply_form.extract(browser_page).fields] == [
        "Full name", "Email", "Resume"]


def test_the_gate_names_an_unreadable_required_control():
    plan = FillPlan(buttons={"submit": (0, 0.95)})
    ok, why = apply_run.can_submit(plan, [], {"auto_apply_submit": True}, {
        "required_empty": [{"label": "Earliest start date", "kind": "unreadable",
                            "why": "closed shadow root", "required": True, "empty": True}]})
    assert not ok and why == ("required field without an answer: Earliest start date (a control "
                              "the run cannot read: closed shadow root)")


def test_the_unreadable_flow_parks_on_the_required_one(_browser, flow_server, tmp_path):
    r = h.run_flow(h.flow("closed_shadow_controls"), jev.FakeJev(), "fake", browser=_browser,
                   server=flow_server, workdir=tmp_path)
    assert r.ok and not r.breaks, (r.status, r.reason, r.breaks)
    unreadable = [d for d in _decisions(r) if d["what"] == "unreadable"]
    assert unreadable and unreadable[0]["required"] == ["Earliest start date"]
