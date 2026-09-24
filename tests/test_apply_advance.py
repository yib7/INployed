"""SP6: advancing and repair.

- The popup guard reads a name (SP5 review round 9): a real question whose
  own words hold confirm, finish, complete, done, send, submit or apply is
  opened; every send shape rounds 4 to 8 found stays refused; a note about
  required marks ("* Required field") is no question and no star.

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

pytest_plugins = ["conftest_browser"]


# === the popup guard reads a name (review round 9) ===================================================

# Real questions the round-8 guard refused (review round 9's table): each is
# a popup the run must open and answer.
_QUESTIONS = {
    "workday_imperative": """<div><button type="button" id="q" aria-haspopup="listbox"
        aria-label="Please confirm you are at least 18 years of age Select One Required"
        >Select One</button></div>""",
    "finish_date": """<div><button type="button" id="q" aria-haspopup="listbox"
        aria-label="Expected finish date">Select</button></div>""",
    "finish_month": """<div><button type="button" id="q" aria-haspopup="listbox"
        aria-label="Finish month">Select</button></div>""",
    "submit_a_source": """<div><label for="q">How did you hear about us?</label>
        <button type="button" id="q" aria-haspopup="listbox" title="Submit a source"
        >Select</button></div>""",
    "apply_placeholder": """<div><button type="button" id="q" aria-haspopup="listbox"
        >Apply a location</button></div>""",
    "willing_to_submit": """<div><button type="button" id="q" aria-haspopup="listbox"
        aria-required="true" aria-label="Willing to submit references">Select One</button></div>""",
    # a question read again after its fill: the shown value is its answer
    "answered_i_confirm": """<div><label for="q">Do you agree to the terms?</label>
        <button type="button" id="q" aria-haspopup="listbox">I confirm</button></div>""",
    "answered_complete": """<div><label for="q">Degree status</label>
        <button type="button" id="q" aria-haspopup="menu">Complete</button></div>""",
    "answered_done": """<div><span id="ql">Background check status</span>
        <button type="button" id="q" aria-haspopup="menu" aria-labelledby="ql">Done</button></div>""",
    "answered_send_by_post": """<div><label for="q">Delivery method</label>
        <button type="button" id="q" aria-haspopup="listbox">Send by post</button></div>""",
    "answered_in_its_box": """<div><span>Degree status</span>
        <button type="button" id="q" aria-haspopup="listbox">Complete</button></div>""",
    "workday_answered": """<div><button type="button" id="q" aria-haspopup="listbox"
        aria-label="Please confirm you are at least 18 years of age I confirm Required"
        >I confirm</button></div>""",
}

# Every send shape rounds 4 to 8 found (and a few beside them): never opened.
_SENDS = {
    "shown_and_aria": """<div><button type="button" id="q" aria-haspopup="menu"
        aria-label="Submit your application right now">Submit your application right now</button>
        </div>""",
    "long_aria": """<div><button type="button" id="q" aria-haspopup="menu"
        aria-label="Submit your application or save a draft">Submit &#9662;</button></div>""",
    "legend_arrow": """<fieldset><legend>Your application *</legend>
        <button type="submit">Submit application</button>
        <button type="button" id="q" aria-haspopup="menu" aria-label="More submit options">
        <svg width="10" height="10"><path d="M0 0 L10 0 L5 8 z"/></svg></button></fieldset>""",
    "icon_six_words": """<div><button type="button" id="q" aria-haspopup="menu"
        aria-label="Choose how to submit your application">
        <svg width="10" height="10"><path d="M0 0 L10 0 L5 8 z"/></svg></button></div>""",
    "apply_with": """<div><button type="button" id="q" aria-haspopup="menu"
        aria-label="Apply with">Apply with</button></div>""",
    "bare_submit_arrow": """<div><button type="button" id="q" aria-haspopup="menu"
        >Submit &#9662;</button></div>""",
    "confirm_and_submit": """<div><button type="button" id="q" aria-haspopup="menu"
        >Confirm and submit</button></div>""",
    "bare_i_confirm": """<div><button type="button" id="q" aria-haspopup="menu"
        >I confirm</button></div>""",
    "labelled_by_the_send": """<div><span id="sl">Submit application</span>
        <button type="button" id="q" aria-haspopup="menu" aria-labelledby="sl">
        <svg width="10" height="10"><path d="M0 0 L10 0 L5 8 z"/></svg></button></div>""",
    "title_finish": """<div><button type="button" id="q" aria-haspopup="menu"
        title="Finish">&#9662;</button></div>""",
}


def _popup(page, markup):
    page.set_content(f"""<body><form>{markup}</form><script>
      document.querySelector('#q').addEventListener('click', () => {{
        document.body.dataset.opened = '1';
        if (document.querySelector('#menu')) return;
        const m = document.createElement('div');
        m.id = 'menu'; m.setAttribute('role', 'listbox');
        m.innerHTML = '<div role="option">Yes</div><div role="option">No</div>';
        document.body.appendChild(m);
      }});</script></body>""")
    return apply_form.Field(n=0, locator=(0, "#q"), label="Q", type="listbox", required=True,
                            widget="popup")


@pytest.mark.parametrize("shape", sorted(_QUESTIONS))
def test_a_real_question_whose_words_hold_a_send_verb_is_opened(browser_page, shape):
    field = _popup(browser_page, _QUESTIONS[shape])
    options = apply_fill.open_listbox_options(browser_page, field)
    assert browser_page.evaluate("document.body.dataset.opened") == "1", shape
    assert options == ["Yes", "No"]


@pytest.mark.parametrize("shape", sorted(_SENDS))
def test_every_send_shape_stays_refused(browser_page, shape):
    field = _popup(browser_page, _SENDS[shape])
    with pytest.raises(apply_fill.PopupRefused):
        apply_fill.open_listbox_options(browser_page, field)
    assert browser_page.evaluate("document.body.dataset.opened") is None, shape


@pytest.mark.parametrize("text, sends", [
    ("Submit", True), ("Submit application", True), ("Submit ▾", True),
    ("More submit options", True), ("Choose how to submit your application", True),
    ("Submit your application or save a draft", True), ("Confirm and submit", True),
    ("Finish", True), ("Done", True), ("I confirm", True), ("Apply", True),
    ("Apply with", True), ("Apply now", True), ("Send", True),
    ("Submit a source", False), ("Expected finish date", False), ("Finish month", False),
    ("Apply a location", False), ("Send by post", False), ("Does not apply", False),
    ("Willing to submit references", False), ("Continue studies", False),
    ("Preferred way to send documents", False), ("Select One", False),
])
def test_a_send_phrase_is_a_send_verb_naming_the_send(text, sends):
    assert apply_fill.send_phrase(text) is sends


def test_a_question_shaped_aria_label_and_its_answer_are_never_read_as_a_name():
    ask = "Please confirm you are at least 18 years of age Select One Required"
    assert apply_fill.popup_refusal({"aria": ask, "shown": "Select One"}) == ""
    assert apply_fill.popup_refusal({"aria": ask, "shown": "I confirm"}) == ""
    assert apply_fill.popup_refusal({"shown": "Done", "labelled": True}) == ""
    assert apply_fill.popup_refusal({"shown": "Done", "boxed": True}) == ""
    # an unlabelled shown send, and a label that names the send, are read
    assert apply_fill.popup_refusal({"shown": "Done"})
    assert apply_fill.popup_refusal({"shown": "", "label": "Submit application",
                                     "labelled": True})


# --- review round 9, Minor: a note about required marks is no question and no star ----------------

def test_a_required_field_note_beside_a_menu_is_no_question_and_no_star(browser_page):
    browser_page.set_content("""<body><form>
      <div class="actions"><p class="note">* Required field</p>
        <button type="submit">Submit application</button>
        <button type="button" aria-haspopup="menu" aria-label="More submit options">
          <svg width="10" height="10"><path d="M0 0 L10 0 L5 8 z"/></svg></button></div>
      <div class="tools"><span>* Required field</span>
        <button type="button" aria-haspopup="menu">More</button></div>
      <div class="tools2"><span>Options for this posting</span>
        <button type="button" aria-haspopup="menu">More</button></div>
      <div class="q"><span>Start month</span>
        <button type="button" aria-haspopup="listbox">Select</button>
        <span class="req" aria-hidden="true">*</span></div>
      <div class="q2"><span>Shift preference</span>
        <button type="button" aria-haspopup="listbox">Select</button>
        <p class="note">Fields marked with * are required</p></div>
      </form></body>""")
    d = apply_form.extract(browser_page)
    assert [(f.label, f.required) for f in d.fields] == [
        ("Start month", True), ("Shift preference", False)]
    assert "More submit options" not in [f.label for f in d.fields]
