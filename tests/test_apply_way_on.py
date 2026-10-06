"""The way on.

- What covers a click put away; a step's Next over a feedback
  Submit; a stranger that took the advance gives way to the form's
  own Next; a sign-up beside a sign-in; sign-ins with another
  site never the way on; controls the run cannot read named and
  parked on.

Headless Chromium through the module-scoped test browser; no network, no
judge but `FakeJev` or a scripted one."""
import sys
from pathlib import Path

import pytest

pytest.importorskip("playwright")

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import apply_click  # noqa: E402
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


# === what covers a click, put away ========================================================================

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


# --- the overlay picker's gaps ---------------------------------------------------------------------------

_TARGET = ('<form id="app"><button type="button" id="go" style="position: fixed; bottom: 40px; right: 40px; '
           'width: 80px; height: 30px">Next</button></form>')
_COVERS = {
    "bare_x": ('<div id="promo" style="position: fixed; inset: 0; background: #eee">Our new app!'
               '<button type="button">×</button></div>', ("close", "×", False)),
    "disagree": ('<div id="didomi-popup" style="position: fixed; inset: 0; background: #eee">'
                 'We and our partners use cookies.<button type="button">Agree and close</button>'
                 '<button type="button">Disagree and close</button></div>',
                 ("consent", "Disagree and close", False)),
    "without_agreeing": ('<div id="cookie-notice" style="position: fixed; inset: 0; '
                         'background: #eee">We use cookies.<button type="button">Agree</button>'
                         '<a href="#">Continue without agreeing</a></div>',
                         ("consent", "Continue without agreeing", False)),
    "necessary_only": ('<div class="cookie-bar" style="position: fixed; inset: 0; background: #eee">'
                       'Cookies help us.<button type="button">Allow all</button>'
                       '<button type="button">Allow necessary only</button></div>',
                       ("consent", "Allow necessary only", False)),
    # the fixed box over the click holds the banner's words; its controls sit
    # in the banner's own root beside it (the JazzHR capture)
    "controls_beside": ('<div id="cookie-consent"><div style="position: fixed; inset: 0; '
                        'background: #eee">This website uses cookies and other analytics '
                        'technologies.</div><div style="position: fixed; top: 0; left: 0">'
                        '<button type="button">Accept All</button><button type="button">Reject '
                        'All</button></div></div>', ("consent", "Reject All", False)),
    # the application's own dialog (Workday's "Start Your Application")
    "own_dialog": ('<div role="dialog" aria-modal="true" style="position: fixed; inset: 0; '
                   'background: #fff"><h2>Start Your Application</h2><button type="button">'
                   'Autofill with Resume</button><button type="button">Apply Manually</button>'
                   '<button type="button" aria-label="Close">×</button></div>',
                   ("none", "", True)),
    # the application's own fixed footer, no dialog; its "Skip this step"
    # is never clicked to put it away. It is the form's by the controls the
    # form owns
    "own_sticky_bar": ('<div class="step-footer" style="position: fixed; inset: 0; '
                       'background: #fff"><button type="button" form="app">Skip this step'
                       '</button><button type="submit" form="app">Submit application</button>'
                       '</div>', ("none", "", True)),
    # a talent-network slide-in and a pre-chat form
    # beside the application hold fields and a submit of their own; each is
    # put away by its own close
    "talent_network": ('<div class="talent-network" style="position: fixed; inset: 0; '
                       'background: #fff"><h3>Join our talent network</h3>'
                       '<label>Name <input type="text"></label>'
                       '<label>Email <input type="email"></label>'
                       '<button type="button">Submit</button>'
                       '<button type="button">No thanks</button></div>',
                       ("close", "No thanks", False)),
    "pre_chat": ('<div id="chat" style="position: fixed; inset: 0; background: #fff">'
                 '<label>Your name <input type="text"></label>'
                 '<label>Your email <input type="email"></label>'
                 '<label>Upload a file <input type="file"></label>'
                 '<button type="button">Start chat</button>'
                 '<button type="button" aria-label="Minimize">_</button></div>',
                 ("close", "_", False)),
    # a close that holds a last-step word is never picked
    "final_word": ('<div id="promo" style="position: fixed; inset: 0; background: #eee">Almost '
                   'there<button type="button">Skip and finish</button></div>',
                   ("none", "", False)),
}


@pytest.mark.parametrize("cover", sorted(_COVERS))
def test_the_overlay_picker_puts_away_each_cover_its_own_way(browser_page, cover):
    html, want = _COVERS[cover]
    browser_page.set_content(f"<body>{_TARGET}{html}</body>")
    found = browser_page.locator("#go").evaluate(apply_click._OVERLAY_JS)
    assert (found["kind"], found["text"], found.get("own")) == want, found
    assert not found["text"].lower().startswith(("accept", "allow all", "agree"))


_FOOTER = ('<div class="step-footer" style="position: fixed; inset: 0; background: #fff">'
           '<button type="button">Skip this step</button>'
           '<button type="button">Submit application</button></div>')
_ALERTS = ('<div class="job-alerts" style="position: fixed; inset: 0; background: #fff">'
           '<label>Keywords <input type="text"></label><label>Email <input type="email"></label>'
           '<button type="button">Upload resume</button>'
           '<button type="button">Maybe later</button></div>')


@pytest.mark.parametrize("box, want", [(_FOOTER, ("none", "", True)),
                                       (_ALERTS, ("close", "Maybe later", False))])
def test_a_fixed_box_is_the_applications_own_by_the_box_it_shares_with_the_fields(
        browser_page, box, want):
    # a page with no form element. A fixed footer in
    # the box that holds the covered control and the application's fields is
    # the application's own; a job-alert slide-in beside that box is put
    # away, though it holds two fields and an upload of its own
    browser_page.set_content(
        '<body><div id="app"><label>Full name <input type="text"></label>'
        '<label>Email <input type="email"></label>'
        '<button type="button" id="go" style="position: fixed; bottom: 40px; right: 40px; '
        'width: 80px; height: 30px">Next</button>' + (box if box is _FOOTER else "") + '</div>'
        + ("" if box is _FOOTER else box) + '</body>')
    found = browser_page.locator("#go").evaluate(apply_click._OVERLAY_JS)
    assert (found["kind"], found["text"], found.get("own")) == want, found


# === the way on: a Next beside another Submit, two forms, sign-ins elsewhere ===========================

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


def test_a_progress_list_that_names_every_step_is_no_step_marker():
    # "Step 1 of 2 ... Step 2 of 2" says nothing of where the
    # page is; the same marker twice (a title and a heading) still does
    progress = "Step 1 of 2: About you\nStep 2 of 2: Documents\nResume *"
    assert apply_run.step_position(_route_digest(progress)) is None
    assert apply_run.step_position(_route_digest("Step 2 of 4\nStep 2 of 4: Documents")) == (2, 4)
    # on the last step with the submit among the fields, the gate, never the Next
    digest = _route_digest(progress, "Next", "Submit")
    plan = FillPlan(buttons={"advance": (0, 0.9), "submit": (1, 0.9)})
    got, button, _ = apply_run.form_route(digest, plan, park_mode=True, submit_apart=False)
    assert (got, button[0]) == ("gate", 1)


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


# === a control the run cannot read ======================================================================

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


# --- a form-associated control that blocks the send without `required` ----------------------------

@pytest.mark.parametrize("name", ["form_associated_invalid", "form_associated_invalid_park"])
def test_a_form_associated_control_its_internals_mark_invalid_parks_naming_it(
        _browser, flow_server, tmp_path, name):
    r = h.run_flow(h.flow(name), jev.FakeJev(), "fake", browser=_browser, server=flow_server,
                   workdir=tmp_path)
    assert r.ok and not r.breaks, (r.status, r.reason, r.breaks)
    assert r.sends == 0 and r.policy is True
    assert not any(a.kind == "click" and a.text == "Submit application" for a in r.actions)


# --- a required unreadable control after forty others ------------------------------------------------

def test_a_required_unreadable_control_after_forty_others_still_parks_the_step(browser_page):
    picks = "".join(f'<x-pick aria-label="Option {i}"></x-pick>' for i in range(45))
    browser_page.set_content(f"""<body><form id="f">
      <label>Full name * <input name="name" required></label>{picks}
      <x-pick aria-label="Preferred shift" aria-required="true"></x-pick>
      <button type="button" id="next">Next</button></form><script>
      customElements.define('x-pick', class extends HTMLElement {{
        constructor() {{ super(); this.attachShadow({{mode: 'closed'}}).innerHTML =
          '<select><option></option><option>Day</option></select>'; }} }});
      </script></body>""")
    digest = apply_form.extract(browser_page)
    n = next(b.n for b in digest.buttons if b.text == "Next")
    run = apply_run._JobRun(apply_run.Runner(jev=jev.FakeJev(), context=None, run_context={},
                                             sleep=lambda s: None), None,
                            {"job_posting_id": "s", "apply_url": "https://x.example/1"})
    run.page = browser_page
    assert len(apply_form.control_scan(browser_page)) == 40      # the cut
    with pytest.raises(apply_run._Parked, match=r"^required field without an answer: Preferred "
                                                r"shift \(a control the run cannot read: closed "
                                                r"shadow root\)$"):
        run._unreadable(digest, n)
