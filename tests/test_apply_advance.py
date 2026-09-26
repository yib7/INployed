"""SP6: advancing and repair.

- The popup guard reads a name (SP5 review round 9): a real question whose
  own words hold confirm, finish, complete, done, send, submit or apply is
  opened; every send shape rounds 4 to 8 found stays refused; a note about
  required marks ("* Required field") is no question and no star.
- Values the page reshapes: a masked phone typed key by key, a phone beside a
  country code as its national digits (FILL-04); a text date in the format
  its box names (FILL-05); a number box's number (FILL-06); an upload widget
  that resets its input, read from its chip and never uploaded twice
  (FILL-01); each verified in code. Escape only while a menu shows (FILL-08).
- The fill's record: how each field was acted on and its error (FILL-15),
  a draft made once per question (FILL-12), a page read again keeping its
  listboxes' options (FILL-09).

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
from apply_judge import FillPlan, PlannedField  # noqa: E402

pytest_plugins = ["conftest_browser"]


def _planned(f, action, value="", option=None, fact_key="x"):
    return PlannedField(n=f.n, locator=f.locator, label=f.label, required=f.required,
                        fact_key=fact_key, value=value, option=option, confidence=1.0,
                        action=action, widget=f.widget, click_locator=f.click_locator,
                        option_locators=list(f.option_locators), options=list(f.options),
                        ident=f.ident)


def _by_label(digest, label):
    found = [f for f in digest.fields if f.label == label]
    assert len(found) == 1, [(f.label, f.type) for f in digest.fields]
    return found[0]


def _fill(page, *planned):
    errors: list = []
    out = apply_fill.apply(page, FillPlan(fields=list(planned)), errors=errors)
    assert not errors, errors
    return {f.n: f.value for f in out}


# === the popup guard reads a name (review round 9) ===================================================

# Real questions the round-8 guard refused (review round 9's table): each is
# a popup the run must open and answer.
_QUESTIONS = {
    "workday_imperative": """<div><button type="button" id="q" aria-haspopup="listbox"
        aria-label="Please confirm you are at least 18 years of age Select One Required"
        >Select One</button></div>""",
    "finish_date": """<div><button type="button" id="q" aria-haspopup="listbox"
        aria-label="Expected finish date">Select</button></div>""",
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
    # the SP6 review's round 9 probes, as it wrote them
    "q9_starred_finish_date": """<div><span>Expected finish date *</span><button type="button"
        id="q" aria-haspopup="listbox" aria-label="Expected finish date">Select</button></div>""",
    "q9_reread_send_by_post_box": """<div><span>Delivery method</span><button type="button"
        id="q" aria-haspopup="listbox">Send by post</button></div>""",
    # SP6 review R2-I3: a last-step verb is read by what follows it, and a
    # label naming a document and the value asked of it is a question
    "r2_finish_month": """<div><button type="button" id="q" aria-haspopup="listbox"
        aria-label="Finish month">Select</button></div>""",
    "r2_starred_finish_month": """<div><span>Finish month *</span><button type="button" id="q"
        aria-haspopup="listbox" aria-label="Finish month">Select</button></div>""",
    "r2_confirm_citizenship": """<div><button type="button" id="q" aria-haspopup="listbox"
        aria-required="true" aria-label="Confirm your citizenship status">Select One</button></div>""",
    "r2_send_by_post_document_delivery": """<div><label for="q">Document delivery</label>
        <button type="button" id="q" aria-haspopup="listbox">Send by post</button></div>""",
    "r2_complete_resume_status": """<div><label for="q">Resume status</label>
        <button type="button" id="q" aria-haspopup="menu">Complete</button></div>""",
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
    # the SP6 review's ten (I1): a leading send verb, or a shown send under a
    # label that names the application, a document, a step or an action
    "n_submit_for_review_box": """<div class="field"><span>Your application</span><button
        type="button" id="q" aria-haspopup="menu">Submit for review</button></div>""",
    "n_submit_for_review_label": """<label for="q">Your application</label><button type="button"
        id="q" aria-haspopup="menu">Submit for review</button>""",
    "n_submit_resume_label": """<label for="q">Resume *</label><button type="button" id="q"
        aria-haspopup="menu">Submit resume</button>""",
    "n_submit_and_continue_box": """<div class="field"><span>Step 3</span><button type="button"
        id="q" aria-haspopup="menu">Submit and continue</button></div>""",
    "n_send_to_recruiter_box": """<div class="field"><span>Share your profile</span><button
        type="button" id="q" aria-haspopup="menu">Send to recruiter</button></div>""",
    "n_submit_label_for": """<label for="q">Application</label><button type="button" id="q"
        aria-haspopup="menu">Submit application</button>""",
    "n_submit_arrow_labelledby_heading": """<h3 id="h">Your application</h3><button
        type="button" id="q" aria-haspopup="menu" aria-labelledby="h">Submit &#9662;</button>""",
    "n_submit_listbox_label": """<label for="q">Your application</label><button type="button"
        id="q" aria-haspopup="listbox">Submit</button>""",
    "n_icon_submit_for_review_box": """<div class="field"><span>Your application</span><button
        type="button" id="q" aria-haspopup="menu" aria-label="Submit for review">
        <svg width="10" height="10"><path d="M0 0 L10 0 L5 8 z"/></svg></button></div>""",
    "n_icon_submit_resume_req": """<div><button type="button" id="q" aria-haspopup="menu"
        aria-required="true" aria-label="Submit resume">
        <svg width="10" height="10"><path d="M0 0 L10 0 L5 8 z"/></svg></button></div>""",
    # SP6 review R2-I3: a last-step verb alone or before the send is refused
    "r2_done": """<div><button type="button" id="q" aria-haspopup="menu">Done</button></div>""",
    "r2_complete_application": """<div><button type="button" id="q" aria-haspopup="menu"
        aria-label="Complete application"><svg width="10" height="10"><path d="M0 0 L10 0 L5 8 z"/>
        </svg></button></div>""",
    "r2_finalize": """<div><button type="button" id="q" aria-haspopup="menu">Finalize</button>
        </div>""",
    # SP6 review R2-M4: a <label for> that names the send, over an icon arrow
    "r2_label_for_the_send": """<div><label for="q">Submit application</label><button
        type="button" id="q" aria-haspopup="menu"><svg width="10" height="10">
        <path d="M0 0 L10 0 L5 8 z"/></svg></button></div>""",
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
    # a submit or send leading a short name, whatever follows (SP6 review I1)
    ("Submit for review", True), ("Submit resume", True), ("Submit and continue", True),
    ("Send to recruiter", True), ("Submit a source", True), ("Send by post", True),
    # a last-step verb by what follows it (SP6 review R2-I3)
    ("Complete application", True), ("Finalize", True), ("Finish now", True),
    ("Finish month", False), ("Confirm your citizenship status", False),
    ("Complete your degree details", False),
    ("Expected finish date", False), ("Apply a location", False), ("Does not apply", False),
    ("Willing to submit references", False), ("Continue studies", False),
    ("Preferred way to send documents", False), ("Select One", False),
])
def test_a_send_phrase_is_a_send_verb_naming_the_send(text, sends):
    assert apply_fill.send_phrase(text) is sends


def test_a_question_shaped_aria_label_and_its_answer_are_never_read_as_a_name():
    ask = "Please confirm you are at least 18 years of age Select One Required"
    assert apply_fill.popup_refusal({"aria": ask, "shown": "Select One"}) == ""
    assert apply_fill.popup_refusal({"aria": ask, "shown": "I confirm"}) == ""
    # under an outside question label the shown text and the title are the answer
    assert apply_fill.popup_refusal({"shown": "Done", "label": "Background check status"}) == ""
    assert apply_fill.popup_refusal({"shown": "Done", "box": "Degree status"}) == ""
    assert apply_fill.popup_refusal({"title": "Submit a source",
                                     "label": "How did you hear about us?"}) == ""
    # an unlabelled shown send, a label that names the application, and a
    # labelling element or a <label for> that names the send, are read
    assert apply_fill.popup_refusal({"shown": "Done"})
    assert apply_fill.popup_refusal({"shown": "Submit", "label": "Your application"})
    assert apply_fill.popup_refusal({"shown": "", "named": "Submit application"})
    assert apply_fill.popup_refusal({"shown": "", "label": "Submit application"})
    # a label that asks is never read as a name (SP6 review R2-M4)
    assert apply_fill.popup_refusal({"shown": "Select", "label": "Expected finish date *"}) == ""
    assert apply_fill.popup_refusal({"shown": "Complete", "label": "Resume status"}) == ""


@pytest.mark.parametrize("text, asks", [
    ("Do you agree to the terms?", True), ("How did you hear about us?", True),
    ("Degree status", True), ("Delivery method", True), ("Background check status", True),
    ("Select all that apply", True), ("Plans after graduation *", True),
    ("Document delivery", True), ("Resume status", True), ("Application source", True),
    ("Your application", False), ("Application", False), ("Resume *", False), ("Step 3", False),
    ("Share your profile", False), ("Submit document type", False), ("", False)])
def test_a_question_label_asks_for_a_value_and_never_names_the_send(text, asks):
    assert apply_fill.question_label(text) is asks


# --- review M2: the extractor reads the send rule too ---------------------------------------------

def test_an_unmarked_question_with_a_send_word_in_its_middle_is_offered(browser_page):
    # "Expected finish date" and "Finish month" by their own aria-labels, no
    # star, no aria-required: fields, opened by discovery (SP6 review
    # R2-I3); an unmarked "Complete application" stays a button
    browser_page.set_content("""<body><form>
      <div><button type="button" id="efd" aria-haspopup="listbox" aria-label="Expected finish date"
        onclick="document.body.dataset.opened = 1">Select</button></div>
      <div><button type="button" id="fm" aria-haspopup="listbox" aria-label="Finish month"
        >Select</button></div>
      <div><button type="button" id="ca" aria-haspopup="menu" aria-label="Complete application"
        >&#9662;</button></div></form></body>""")
    d = apply_form.extract(browser_page)
    assert [(f.label, f.required) for f in d.fields] == [("Expected finish date", False),
                                                        ("Finish month", False)]
    assert "#ca" in [b.locator[1] for b in d.buttons]
    apply_fill.open_listbox_options(browser_page, d.fields[0])
    assert browser_page.evaluate("document.body.dataset.opened") == "1"


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


def test_a_note_that_ends_in_a_parenthesised_star_is_no_question(browser_page):
    # final review B-M5: "Required fields are marked with an asterisk (*)"
    # beside an unlabelled menu is a note, never the menu's question, starred
    # or not (the starred one parked as a required field without an answer)
    browser_page.set_content("""<body><form>
      <div class="tools"><span>* Required fields are marked with an asterisk (*)</span>
        <button type="button" aria-haspopup="menu">Tools</button></div>
      <div class="tools2"><span>Required fields are marked with an asterisk (*)</span>
        <button type="button" aria-haspopup="true">View</button></div>
      <div class="q"><span>Start month</span>
        <button type="button" aria-haspopup="listbox">Select</button>
        <span class="req" aria-hidden="true">*</span></div>
      </form></body>""")
    d = apply_form.extract(browser_page)
    assert [(f.label, f.required) for f in d.fields] == [("Start month", True)]


# === values the page reshapes (FILL-01, FILL-04, FILL-05, FILL-06, FILL-08) =============================

def test_a_masked_phone_is_typed_key_by_key_and_verified_by_its_digits(browser_page, fixture_url):
    browser_page.goto(fixture_url("masked_phone.html"))
    d = apply_form.extract(browser_page)
    phone, mobile = _by_label(d, "Phone"), _by_label(d, "Mobile number")
    got = _fill(browser_page, _planned(phone, "fill", "555-555-0100", fact_key="phone"),
                _planned(mobile, "fill", "555-555-0100", fact_key="phone"))
    # the mask drew the keys; the box beside the country code holds bare digits
    assert got[phone.n] == "(555) 555-0100"
    assert got[mobile.n] == "5555550100"
    assert browser_page.evaluate("document.getElementById('mobile').checkValidity()")
    for n in (phone.n, mobile.n):
        assert apply_run.shaped_holds(got[n], "phone", "555-555-0100")
    assert not apply_run.shaped_holds("(555) 555-0199", "phone", "555-555-0100")


def test_a_text_date_takes_the_format_its_box_names(browser_page):
    browser_page.set_content("""<body><form>
      <label>Date (MM/DD/YYYY) <input id="a" type="text"></label>
      <label>Start <input id="b" type="text" placeholder="DD/MM/YYYY"></label>
      <label>Signed on <input id="c" type="text" pattern="[0-9]{2}/[0-9]{2}/[0-9]{4}"></label>
      <label>Plain <input id="e" type="text"></label>
      </form></body>""")
    d = apply_form.extract(browser_page)
    boxes = {f.locator[1]: f for f in d.fields}
    got = _fill(browser_page, *[_planned(boxes[k], "fill", "2026-09-24") for k in
                                ("#a", "#b", "#c", "#e")])
    assert [got[boxes[k].n] for k in ("#a", "#b", "#c", "#e")] == [
        "09/24/2026", "24/09/2026", "09/24/2026", "2026-09-24"]
    assert all(apply_run.shaped_holds(got[boxes[k].n], "date", "2026-09-24")
               for k in ("#a", "#c", "#e"))


def test_a_number_box_takes_the_number(browser_page):
    browser_page.set_content("""<body><form>
      <label>Expected salary <input id="s" type="number"></label>
      <label>Years of experience <input id="y" type="number"></label></form></body>""")
    d = apply_form.extract(browser_page)
    s, y = _by_label(d, "Expected salary"), _by_label(d, "Years of experience")
    # moved on purpose (cycle 18, FM-5): a value that is no plain number
    # leaves the box blank ("$120,000" was 120000 and "5+" was 5)
    errors: list = []
    out = apply_fill.apply(browser_page, FillPlan(fields=[
        _planned(s, "fill", "$120,000"), _planned(y, "fill", "5+")]), errors=errors)
    assert [f.value for f in out] == ["", ""]
    assert [e["n"] for e in errors] == [s.n, y.n]
    got = _fill(browser_page, _planned(s, "fill", "120000"), _planned(y, "fill", "5.5"))
    assert (got[s.n], got[y.n]) == ("120000", "5.5")


def test_an_upload_the_widget_consumed_is_read_from_its_chip_and_never_sent_twice(
        browser_page, fixture_url, tmp_path):
    pdf = tmp_path / "Jane_Doe_Resume.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
    browser_page.goto(fixture_url("upload_resets_input.html"))
    d = apply_form.extract(browser_page)
    resume = _by_label(d, "Resume")
    assert resume.required
    pf = _planned(resume, "upload", str(pdf), fact_key="resume_file")
    got = _fill(browser_page, pf)
    assert got[resume.n] == "Jane_Doe_Resume.pdf"
    assert browser_page.evaluate("document.getElementById('resume').files.length") == 0
    assert apply_run.shaped_holds(got[resume.n], "upload", str(pdf))
    # the same plan again (a retry, a page read again): never a second upload
    again = _fill(browser_page, pf)
    assert again[resume.n] == "Jane_Doe_Resume.pdf"
    assert browser_page.evaluate("document.body.dataset.uploads") == "1"


def test_escape_is_pressed_only_while_a_menu_shows(browser_page, fixture_url):
    browser_page.goto(fixture_url("modal_with_combobox.html"))
    d = apply_form.extract(browser_page)
    city = _by_label(d, "City")
    # the typeahead says expanded after its click and shows nothing: no Escape
    assert apply_fill.open_listbox_options(browser_page, city) == []
    assert browser_page.locator("#dialog").count() == 1
    got = _fill(browser_page, _planned(city, "fill", "Anytown", fact_key="address_city"))
    assert got[city.n] == "Anytown, CA"
    assert browser_page.locator("#dialog").count() == 1
    # a menu that shows its options takes the Escape
    browser_page.set_content("""<body><div role="dialog" aria-modal="true" id="dlg"><form>
      <div><label for="m">Team</label><button type="button" id="m" aria-haspopup="menu"
        onclick="document.getElementById('menu').hidden = false">Select...</button>
      <div role="menu" id="menu" hidden><div role="menuitemradio">Data</div>
        <div role="menuitemradio">Platform</div></div></div></form></div>
      <script>document.addEventListener('keydown', (e) => {
        if (e.key !== 'Escape') return;
        const m = document.getElementById('menu');
        if (!m.hidden) { m.hidden = true; return; }
        document.getElementById('dlg').remove(); });</script></body>""")
    team = _by_label(apply_form.extract(browser_page), "Team")
    assert apply_fill.open_listbox_options(browser_page, team) == ["Data", "Platform"]
    assert browser_page.locator("#menu").is_hidden()
    assert browser_page.locator("#dlg").count() == 1


def test_a_reshaped_value_is_verified_in_code_never_by_the_judge(tmp_path):
    from unittest.mock import Mock

    class Strict:
        asked: list = []

        def judge(self, state, questions):
            self.asked.append(sorted(questions))
            return {qid: jev.Answer(kind="noul", noul=0.1) for qid in questions}

    judge = Strict()
    run = apply_run._JobRun(apply_run.Runner(jev=judge, context=Mock(), run_context={},
                                             sleep=lambda s: None), Mock(),
                            {"job_posting_id": "s", "apply_url": "https://x.example/1"})
    digest = apply_form.FormDigest("x.example", "Apply", "", fields=[
        apply_form.Field(0, (0, "#p"), "Phone", "tel", True),
        apply_form.Field(1, (0, "#d"), "Date (MM/DD/YYYY)", "text", True),
        apply_form.Field(2, (0, "#r"), "Resume", "file", True)])
    rows = [("phone", "fill", "555-555-0100"), ("today", "fill", "2026-09-24"),
            ("resume_file", "upload", str(tmp_path / "Jane_Doe_Resume.pdf"))]
    plan = FillPlan(fields=[PlannedField(n=i, locator=f.locator, label=f.label, required=True,
                                         fact_key=k, value=v, option=None, confidence=1.0,
                                         action=a)
                            for i, (f, (k, a, v)) in enumerate(zip(digest.fields, rows))])
    filled = [apply_fill.Filled(0, "Phone", "(555) 555-0100"),
              apply_fill.Filled(1, "Date (MM/DD/YYYY)", "09/24/2026"),
              apply_fill.Filled(2, "Resume", "Jane_Doe_Resume.pdf")]
    got = run._verify(filled, {}, {}, apply_run._shaped(plan, digest))
    assert [v.ok for v in got] == [True, True, True]
    assert judge.asked == []
    # a value the page changed is caught in code as well
    wrong = [apply_fill.Filled(0, "Phone", "(555) 555-0199"),
             apply_fill.Filled(1, "Date (MM/DD/YYYY)", "09/25/2026"),
             apply_fill.Filled(2, "Resume", "")]
    assert [v.ok for v in run._verify(wrong, {}, {}, apply_run._shaped(plan, digest))] == [
        False, False, False]


def test_a_pasted_cover_letter_and_a_search_boxs_match_are_verified_in_code(tmp_path):
    # SP8b, live 2026-09-25: the judge read the cover letter's read-back, the
    # sheet's own words with its line breaks folded, at 0.79 (the gate needs
    # 0.80) and parked native_required_submit on "could not verify"; it read
    # "Anytown, California, United States", the match a City list box took
    # for "Anytown", at 0.20 and Lever's "Anytown, CA, United States" for
    # "Anytown, CA" at 0.44. A string comparison settles all three.
    from unittest.mock import Mock

    import apply_facts

    class Strict:
        asked: list = []

        def judge(self, state, questions):
            self.asked.append(sorted(questions))
            return {qid: jev.Answer(kind="noul", noul=0.1) for qid in questions}

    judge = Strict()
    run = apply_run._JobRun(apply_run.Runner(jev=judge, context=Mock(), run_context={},
                                             sleep=lambda s: None), Mock(),
                            {"job_posting_id": "s", "apply_url": "https://x.example/1"})
    run.catalog = apply_facts.FactCatalog([
        apply_facts.Fact("location", "Anytown, CA", ""),
        apply_facts.Fact("address_city", "Anytown", ""),
        apply_facts.Fact("address_state", "California", ""),
        apply_facts.Fact("address_country", "United States", "")])
    letter = "Dear hiring team,\n\nI am writing to apply."
    digest = apply_form.FormDigest("x.example", "Apply", "", fields=[
        apply_form.Field(0, (0, "#c"), "Cover letter", "textarea", True, widget="editable"),
        apply_form.Field(1, (0, "#city"), "City", "listbox", True),
        apply_form.Field(2, (0, "#loc"), "Current location", "listbox", True,
                         widget="typeahead")])
    rows = [("cover_letter_text", letter), ("address_city", "Anytown"),
            ("location", "Anytown, CA")]
    plan = FillPlan(fields=[PlannedField(n=i, locator=f.locator, label=f.label, required=True,
                                         fact_key=k, value=v, option=None, confidence=1.0,
                                         action="fill", widget=f.widget)
                            for i, (f, (k, v)) in enumerate(zip(digest.fields, rows))])
    shaped = apply_run._shaped(plan, digest)
    assert [kind for kind, _ in shaped.values()] == ["text", "suggestion", "suggestion"]
    filled = [apply_fill.Filled(0, "Cover letter", "Dear hiring team, I am writing to apply."),
              apply_fill.Filled(1, "City", "Anytown, California, United States"),
              apply_fill.Filled(2, "Current location", "Anytown, CA, United States")]
    assert [v.ok for v in run._verify(filled, {}, {}, shaped)] == [True, True, True]
    assert judge.asked == []
    # a letter the box cut is caught in code; a match that names another
    # place is the judge's to read, and it says no here
    wrong = [apply_fill.Filled(0, "Cover letter", "Dear hiring team,"),
             apply_fill.Filled(1, "City", "Anytown, Texas, United States"),
             apply_fill.Filled(2, "Current location", "Springfield, IL")]
    assert [v.ok for v in run._verify(wrong, {}, {}, shaped)] == [False, False, False]
    assert judge.asked == [["placeholder_1", "placeholder_2", "verify_1", "verify_2"]]


def test_a_search_boxs_match_names_the_value_typed():
    import apply_facts
    places = apply_run.place_words(apply_facts.FactCatalog([
        apply_facts.Fact("location", "Anytown, CA", ""),
        apply_facts.Fact("address_city", "Anytown", ""),
        apply_facts.Fact("address_state", "California", ""),
        apply_facts.Fact("address_country", "United States", "")]))
    holds = apply_run.suggestion_holds
    assert holds("Anytown, CA", "Anytown, CA", places)
    assert holds("Anytown, California, United States", "Anytown, CA", places)
    assert holds("Anytown, California, United States", "Anytown", places)
    assert holds("Anytown, CA, USA", "Anytown", places)
    assert not holds("Anytown, Texas", "Anytown, CA", places)
    assert not holds("Anytown, Texas", "Anytown", places)        # a state not the candidate's
    assert not holds("Springfield, IL", "Anytown, CA", places)
    assert not holds("Anytownship, CA", "Anytown", places)
    assert not holds("", "Anytown", places)


def test_the_upload_reset_flow_uploads_once_and_reaches_the_gate(_browser, flow_server, tmp_path):
    r = h.run_flow(h.flow("upload_resets_input"), jev.FakeJev(), "fake", browser=_browser,
                   server=flow_server, workdir=tmp_path)
    assert r.ok and not r.breaks, (r.status, r.reason, r.breaks)
    assert [a.kind for a in r.actions].count("upload") == 1


# === the fill's record (FILL-09, FILL-12, FILL-15) ==============================================

def test_each_fields_act_is_recorded_with_how_and_its_error(browser_page, _browser, flow_server,
                                                           tmp_path):
    # FILL-15: the fill says how it acted on each field and the error's type
    browser_page.set_content("""<body><form><label>Phone <input id="p" type="tel"
      placeholder="(___) ___-____"></label><label>Name <input id="n"></label></form></body>""")
    d = apply_form.extract(browser_page)
    phone, name = _by_label(d, "Phone"), _by_label(d, "Name")
    gone = PlannedField(n=9, locator=(0, "#nowhere"), label="Gone", required=False,
                        fact_key="x", value="x", option=None, confidence=1.0, action="fill")
    outcomes: list = []
    apply_fill.apply(browser_page, FillPlan(fields=[
        _planned(phone, "fill", "555-555-0100"), _planned(name, "fill", "Jane"), gone]),
        outcomes=outcomes)
    assert [(o["label"], o["how"], o["error"]) for o in outcomes] == [
        ("Phone", "phone digits key by key", ""), ("Name", "fill", ""), ("Gone", "", "LookupError")]
    # and the record carries them
    r = h.run_flow(h.flow("masked_phone"), jev.FakeJev(), "fake", browser=_browser,
                   server=flow_server, workdir=tmp_path)
    record = (Path(r.trace).parent.parent / apply_run.RECORD_NAME).read_text(encoding="utf-8")
    assert "- Fill outcomes:" in record and "  - Phone: phone digits key by key" in record
    assert "  - Mobile number: phone national digits" in record


def test_a_draft_is_made_once_per_question_and_reused_when_the_page_comes_back(tmp_path):
    from unittest.mock import Mock
    import apply_facts

    class Gen:
        calls = 0
        last = None

        def answer(self, field, catalog, judge, *, budget):
            Gen.calls += 1
            return "Two years of ingestion pipelines at Acme Corp."
    runner = apply_run.Runner(jev=jev.FakeJev(), context=Mock(), run_context={},
                              sleep=lambda s: None, answergen=Gen())
    run = apply_run._JobRun(runner, Mock(), {"job_posting_id": "s",
                                             "apply_url": "https://x.example/1"})
    run.catalog = apply_facts.build(h.write_job_folder(tmp_path / "job"), answers=h.bank())
    essay = apply_form.Field(0, (0, "#q"), "Why this role?", "textarea", True,
                             help="Max 500 characters.")
    digest = apply_form.FormDigest("x.example", "Apply", "", fields=[essay])

    def plan():
        return FillPlan(fields=[PlannedField(n=0, locator=essay.locator, label=essay.label,
                                             required=True, fact_key="needs_generation",
                                             value="", option=None, confidence=0.9,
                                             action="generate")])
    pages = []
    for _ in range(2):
        rec = {"generated": []}
        p = plan()
        run._resolve_generation(digest, p, rec)
        assert p.fields[0].action == "fill" and p.fields[0].value.startswith("Two years")
        pages.append(rec)
    assert Gen.calls == 1 and run.gen_budget == apply_run.GENERATE_MAX - 1
    assert pages[1]["generated"][0]["reused"] is True
    assert apply_run.generated_count(pages) == 1


def test_a_page_read_again_takes_its_listboxes_options_without_opening_them(browser_page):
    from unittest.mock import Mock
    # the menu is drawn on the click and taken away when it closes
    browser_page.set_content("""<body><form><div><label for="t">Team</label>
      <button type="button" id="t" aria-haspopup="listbox">Select...</button></div></form>
      <script>
      document.getElementById('t').addEventListener('click', () => {
        window.opens = (window.opens || 0) + 1;
        const m = document.createElement('div');
        m.id = 'm'; m.setAttribute('role', 'listbox');
        m.innerHTML = '<div role="option">Data</div><div role="option">Platform</div>';
        document.body.appendChild(m);
      });
      document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && document.getElementById('m')) document.getElementById('m').remove();
      });</script></body>""")
    run = apply_run._JobRun(apply_run.Runner(jev=jev.FakeJev(), context=Mock(), run_context={},
                                             sleep=lambda s: None), Mock(),
                            {"job_posting_id": "s", "apply_url": "https://x.example/1"})
    run.page = browser_page
    for _ in range(2):
        d = apply_form.extract(browser_page)
        assert _by_label(d, "Team").options == []
        run._discover_listbox_options(d)
        assert _by_label(d, "Team").options == ["Data", "Platform"]
    assert browser_page.evaluate("window.opens") == 1




# --- SP6 review I5: a file of the same name the page showed before this run's upload ---------------

def test_a_kept_resume_of_the_same_name_never_stands_for_this_jobs_upload(
        _browser, flow_server, tmp_path):
    r = h.run_flow(h.flow("upload_profile_kept"), jev.FakeJev(), "fake", browser=_browser,
                   server=flow_server, workdir=tmp_path)
    assert r.ok and not r.breaks, (r.status, r.reason, r.breaks)
    assert [a.kind for a in r.actions].count("upload") == 1


def test_an_upload_is_skipped_only_after_this_run_uploaded_and_saw_it(browser_page, tmp_path):
    pdf = tmp_path / "Jane_Doe_Resume.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
    browser_page.set_content((REPO / "tests" / "fixtures" / "forms" /
                              "upload_profile_kept.html").read_text(encoding="utf-8"))
    d = apply_form.extract(browser_page)
    resume = _by_label(d, "Resume")
    pf = _planned(resume, "upload", str(pdf), fact_key="resume_file")
    # the kept chip names the file already: never this run's upload, nor verified by it
    assert apply_fill.upload_read({"file": "", "text": "Jane_Doe_Resume.pdf (uploaded)"},
                                  "Jane_Doe_Resume.pdf",
                                  "Jane_Doe_Resume.pdf (uploaded)") == ""
    assert not apply_fill.upload_shown(browser_page, browser_page.locator("#resume"), pf)
    assert _fill(browser_page, pf)[resume.n] == "Jane_Doe_Resume.pdf"
    assert browser_page.evaluate("document.body.dataset.uploads") == "1"
    # the same plan again (a retry, the page read again): this run's verified upload stands
    assert _fill(browser_page, pf)[resume.n] == "Jane_Doe_Resume.pdf"
    assert browser_page.evaluate("document.body.dataset.uploads") == "1"


# --- SP6 review R2-M1: a widget that replaces the kept chip in place --------------------------------------

def test_an_upload_that_replaced_a_kept_chip_of_its_name_is_verified_and_made_once(
        _browser, flow_server, tmp_path):
    r = h.run_flow(h.flow("upload_profile_replace"), jev.FakeJev(), "fake", browser=_browser,
                   server=flow_server, workdir=tmp_path)
    assert r.ok and not r.breaks, (r.status, r.reason, r.breaks)
    assert [a.kind for a in r.actions].count("upload") == 1


@pytest.mark.parametrize("before, now, got", [
    # the box's words changed and still name the file: the upload shows
    ("Resume * Jane_Doe_Resume.pdf (uploaded 2026-08-02)", "Resume * Jane_Doe_Resume.pdf",
     "Jane_Doe_Resume.pdf"),
    # unchanged: no evidence of this upload
    ("Resume * Jane_Doe_Resume.pdf (uploaded 2026-08-02)",
     "Resume * Jane_Doe_Resume.pdf (uploaded 2026-08-02)", ""),
    # changed, but into an error beside the kept name: no evidence either
    ("Resume * Jane_Doe_Resume.pdf (uploaded 2026-08-02)",
     "Resume * Jane_Doe_Resume.pdf (uploaded 2026-08-02) The file could not be uploaded", ""),
])
def test_an_upload_read_back_takes_a_change_that_still_names_the_file(before, now, got):
    assert apply_fill.upload_read({"file": "", "text": now}, "Jane_Doe_Resume.pdf", before) == got
