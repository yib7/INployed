"""SP5: every widget family the layout study found, read as a person sees it
and filled so the page holds what was planned.

Per family: the extractor's reading (count, type, label, required, options,
widget), a fill and read-back round trip through `apply_fill.apply`, and
(in `apply_harness.FLOWS`) a replica flow in the matrix. The replicas under
`tests/fixtures/forms/` are synthetic: the shapes of the captured pages,
invented wording.

- G2 labels: marker-only text (a star in its own span, "(required)", an
  sr-only "Required", an aria-hidden star, a leading star) is skipped and
  sets `required`; hidden text inside a label is left out; a file box takes
  its group's question, never its "Attach" or file-count label; a generic
  aria-label ("Search", "Select...", "textbox") falls through to the visible
  question; label[for] counts only for a unique id whose control it is.
- G3 junk: honeypots, react-select's hidden required twin, a read-only box
  and a posting's job-alert widget are no fields; the harness sees a fill
  into one as an invariant break.
- G6 hidden natives behind a visible label or trigger: kept, acted on
  through the label or trigger (`click_locator`).
- G7 custom single choices: a role=radio group, Yes / No buttons with
  aria-pressed, a dropdown drawn as a button (listbox or menu), an untyped
  typeahead; "Click here" and "-- No answer --" placeholders dropped.
- G8 a question's tick boxes are one field with the question's words.
- G12 open shadow roots are walked, their controls located `<host> >> <inner>`.
- G10 a disabled submit is kept with its flag, and so is a primary one.
- EXT-07 a list past 40 is matched in code; EXT-10 react-select's pick is
  read from its sibling; EXT-12 date parts; EXT-13 a rich-text box;
  EXT-19 / FILL-02 a stable locator and the control's identity checked
  before the act; FILL-07 the option aliases; FILL-13 picks verified in code;
  READ-05 the section headings sent with the fields.

Headless Chromium through the module-scoped test browser, the fixtures over
the local fixture server; no network, no judge but `FakeJev`."""
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("playwright")

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import apply_facts  # noqa: E402
import apply_fill  # noqa: E402
import apply_form  # noqa: E402
import apply_harness as h  # noqa: E402
import apply_judge  # noqa: E402
import apply_run  # noqa: E402
import jev  # noqa: E402
from apply_judge import FillPlan, PlannedField  # noqa: E402

pytest_plugins = ["conftest_browser"]


def _open(page, fixture_url, name, *clicks, wait_ms=900):
    page.set_viewport_size({"width": 1400, "height": 1000})
    page.goto(fixture_url(name))
    page.wait_for_timeout(wait_ms)
    for sel in clicks:
        page.locator(sel).first.click()
        page.wait_for_timeout(300)
    return apply_form.extract(page)


def _by_label(digest, label):
    found = [f for f in digest.fields if f.label == label]
    assert len(found) == 1, [(f.label, f.type) for f in digest.fields]
    return found[0]


def _planned(f, action, value="", option=None):
    return PlannedField(n=f.n, locator=f.locator, label=f.label, required=f.required,
                        fact_key="x", value=value, option=option, confidence=1.0, action=action,
                        widget=f.widget, click_locator=f.click_locator,
                        option_locators=list(f.option_locators), options=list(f.options),
                        ident=f.ident)


def _fill(page, *planned):
    errors: list = []
    out = apply_fill.apply(page, FillPlan(fields=list(planned)), errors=errors)
    assert not errors, errors
    return {f.n: f.value for f in out}


# --- G2: labels a person reads, required markers ------------------------------------------------

def test_marker_only_text_is_skipped_and_marks_the_field_required(browser_page, fixture_url):
    d = _open(browser_page, fixture_url, "lever_cards.html")
    labels = [(f.label, f.required) for f in d.fields]
    # the star sits in its own span after the question (Lever's ✱)
    assert ("Are you legally allowed to take up employment in the United States?", True) in labels
    assert ("How did you hear about this job?", True) in labels
    # a hidden error inside a label never joins it
    assert ("Current location", True) in labels and ("Resume/CV", True) in labels
    d = _open(browser_page, fixture_url, "paylocity_required_span.html")
    assert [(f.label, f.required) for f in d.fields][:4] == [
        ("First Name", True), ("Last Name", True), ("Email", True), ("Mobile Phone", False)]
    d = _open(browser_page, fixture_url, "teamtailor_modal.html", "#decline-cookies", "#apply")
    got = {f.label: f.required for f in d.fields}
    # a star plus an sr-only "Required"; an sr-only "Required." before a consent
    assert got["First name"] and got["Email"] and not got["Phone"]
    assert got["I confirm that the information in this application is accurate"]
    d = _open(browser_page, fixture_url, "greenhouse_react_select.html")
    # an aria-hidden star is still a star the person sees
    assert _by_label(d, "Will you now or in the future require visa sponsorship?").required


def test_a_file_box_takes_its_questions_words_never_its_buttons(browser_page, fixture_url):
    d = _open(browser_page, fixture_url, "greenhouse_react_select.html")
    resume = next(f for f in d.fields if f.type == "file")
    assert (resume.label, resume.required) == ("Resume/CV", True)      # never "Attach"
    d = _open(browser_page, fixture_url, "rippling_generic_aria.html")
    resume = next(f for f in d.fields if f.type == "file")
    assert (resume.label, resume.required) == ("Resume", True)         # never "Total 0 file..."
    d = _open(browser_page, fixture_url, "bamboo_honeypot_mui.html", "#apply")
    resume = next(f for f in d.fields if f.type == "file")
    assert resume.label == "Resume"                                     # never "file-input"


def test_an_uploads_own_words_and_a_step_marker_never_name_its_box(browser_page):
    browser_page.set_content("""<body><form>
      <div class="field"><label for="cv">Upload CV<sup aria-hidden="true">*</sup></label>
        <div class="zone"><span>Drop your file or <u>upload</u></span>
        <input type="file" id="cv" aria-label="Drop your file or upload, Upload CV"
               style="position:absolute;opacity:0;width:100%;height:100%"></div></div>
      <div class="step"><p>Step 1 of 3</p><div>
        <button type="button">Select Resume to Upload</button>
        <input type="file" id="res" style="display:none"></div></div>
      <div class="field" role="group"><div>Portfolio</div>
        <label for="pf">Total 0 file selected</label><input type="file" id="pf"></div>
      </form></body>""")
    d = apply_form.extract(browser_page)
    assert [(f.label, f.required) for f in d.fields] == [
        ("Upload CV", True), ("Select Resume to Upload", False), ("Portfolio", False)]


def test_a_text_box_inside_a_dropdown_drawn_as_a_box_is_that_dropdown(browser_page):
    # Paylocity's State: a box with aria-haspopup=listbox showing its value,
    # a text box inside it that opens the list; the label wraps them both
    browser_page.set_content("""<body><form>
      <label for="st"><span>State</span><span>*</span>
        <div id="wrap" aria-haspopup="listbox" aria-expanded="false">
          <div class="input-select-single-value">Select a state</div>
          <input id="st" type="text" aria-autocomplete="list" required></div></label>
      <div id="list" role="listbox" hidden>
        <div role="option">Arizona</div><div role="option">California</div></div>
      <script>
        const box = document.getElementById('st'), list = document.getElementById('list');
        box.addEventListener('click', () => { list.hidden = false; });
        list.querySelectorAll('[role=option]').forEach((o) => o.addEventListener('click', () => {
          document.querySelector('.input-select-single-value').textContent = o.textContent;
          box.value = ''; list.hidden = true; }));
      </script></form></body>""")
    d = apply_form.extract(browser_page)
    assert [(f.label, f.type, f.widget, f.required) for f in d.fields] == [
        ("State", "listbox", "combo", True)]
    state = d.fields[0]
    state.options = apply_fill.open_listbox_options(browser_page, state)
    assert state.options == ["Arizona", "California"]
    values = _fill(browser_page, _planned(state, "select", "California", "California"))
    assert values[state.n] == "California"


def test_a_resume_parsers_upload_is_no_question_of_the_application(browser_page, fixture_url,
                                                                   tmp_path):
    d = _open(browser_page, fixture_url, "ashby_yesno.html")
    files = [f for f in d.fields if f.type == "file"]
    assert [(f.label, f.required) for f in files][1:] == [("Resume", True)]
    parser = files[0]
    assert parser.help == apply_judge.AUTOFILL_PARSER and not parser.required
    catalog = apply_facts.build(h.write_job_folder(tmp_path / "job"), answers=h.bank())
    plan = apply_judge.plan(d, catalog, {})
    by_n = {pf.n: pf for pf in plan.fields}
    assert by_n[parser.n].action == "skip" and by_n[files[1].n].action == "upload"


def test_a_generic_aria_label_falls_through_to_the_visible_question(browser_page, fixture_url):
    d = _open(browser_page, fixture_url, "rippling_generic_aria.html")
    labels = [f.label for f in d.fields]
    assert labels[:5] == ["First name", "Last name", "Email", "Location", "Gender"]
    assert not {"Search", "Select...", "textbox"} & set(labels)


def test_an_id_shared_by_every_option_never_labels_a_radio(browser_page, fixture_url):
    d = _open(browser_page, fixture_url, "ashby_yesno.html")
    relocate = _by_label(d, "Are you willing to relocate for this role?")
    assert (relocate.type, relocate.required) == ("radio", True)
    assert relocate.options == ["Yes, I am open to it", "No, not at this time"]
    values = _fill(browser_page, _planned(relocate, "select", "Yes", "No, not at this time"))
    assert values[relocate.n] == "No, not at this time"


def test_a_label_for_a_shared_id_never_names_another_questions_box(browser_page):
    browser_page.set_content("""<body><form>
      <div class="q"><label for="q">What is your notice period?</label><input id="q" name="a"></div>
      <div class="q"><span>Which team would you like to join? *</span><input id="q" name="b"></div>
      </form></body>""")
    d = apply_form.extract(browser_page)
    assert [(f.label, f.required) for f in d.fields] == [
        ("What is your notice period?", False), ("Which team would you like to join?", True)]


# --- G3: boxes no person fills -------------------------------------------------------------------

def test_honeypots_read_only_boxes_and_hidden_twins_are_no_fields(browser_page, fixture_url):
    d = _open(browser_page, fixture_url, "workday_create_account.html")
    assert [f.label for f in d.fields] == ["Email Address", "Password", "Verify New Password"]
    d = _open(browser_page, fixture_url, "oracle_email_terms.html")
    assert "honeypot" not in [f.label for f in d.fields] and len(d.fields) == 2
    d = _open(browser_page, fixture_url, "bamboo_honeypot_mui.html")
    assert d.fields == []                   # the posting's read-only "Link to This Job"
    d = _open(browser_page, fixture_url, "bamboo_honeypot_mui.html", "#apply")
    assert "Please leave this field blank" not in [f.label for f in d.fields]
    d = _open(browser_page, fixture_url, "greenhouse_react_select.html")
    # react-select's hidden required twins (opacity 0, tabindex -1, aria-hidden)
    assert [f.type for f in d.fields].count("text") == 2
    browser_page.set_content("""<body><h1>Analytics Engineer</h1><p>Apply below.</p>
      <label for="alert">Select how often (in days) to receive an alert:</label>
      <input id="alert" type="number" required><button>Apply now</button></body>""")
    assert apply_form.extract(browser_page).fields == []


def test_the_harness_breaks_on_a_fill_into_a_honeypot_or_a_read_only_box(browser_page):
    browser_page.set_content("""<body><form>
      <label for="site">Leave this field blank</label><input id="site" type="text">
      <label for="share">Link to this job</label><input id="share" type="text" readonly>
      <label for="name">Name</label><input id="name" type="text"></form></body>""")
    rec = h.Recorder(None)
    with rec.recording():
        browser_page.locator("#site").fill("x")
        browser_page.locator("#name").fill("Jane")
        browser_page.locator("#share").fill("y", force=True)
    out = type("O", (), {"status": "needs_human", "reason": "x"})()
    breaks = h.invariant_breaks(out, rec, h.Sends(rec))
    assert [b.split(" (")[0] for b in breaks] == ["JUNK-FILL: a fill into a box no person fills"] * 2
    assert "honeypot" in breaks[0] and "read-only" in breaks[1]


# --- G6: hidden natives behind a visible label or trigger ----------------------------------------

def test_a_zero_size_terms_box_is_ticked_through_its_label(browser_page, fixture_url):
    d = _open(browser_page, fixture_url, "oracle_email_terms.html")
    terms = _by_label(d, "I agree with the terms and conditions of the application")
    assert (terms.type, terms.required, terms.options) == ("checkbox", True, ["checked"])
    assert terms.click_locator is not None
    values = _fill(browser_page, _planned(terms, "select", "yes", "checked"))
    assert values[terms.n] == "checked"
    assert browser_page.locator("#legal-disclaimer").is_checked()


def test_hidden_radios_behind_their_labels_are_one_question(browser_page):
    browser_page.set_content("""<body><form><p>Do you need a visa to work here? *</p>
      <div class="opts">
      <input type="radio" id="v1" name="visa" value="1" style="position:absolute;width:0;height:0;opacity:0">
      <label for="v1">Yes, I need one</label>
      <input type="radio" id="v2" name="visa" value="2" style="position:absolute;width:0;height:0;opacity:0">
      <label for="v2">No, I do not</label></div></form></body>""")
    d = apply_form.extract(browser_page)
    visa = _by_label(d, "Do you need a visa to work here?")
    assert (visa.type, visa.required, visa.options) == ("radio", True,
                                                        ["Yes, I need one", "No, I do not"])
    assert len(visa.option_locators) == 2
    values = _fill(browser_page, _planned(visa, "select", "No", "No, I do not"))
    assert values[visa.n] == "No, I do not" and browser_page.locator("#v2").is_checked()


def test_a_hidden_select_behind_a_styled_trigger_is_picked_in_place(browser_page, fixture_url):
    d = _open(browser_page, fixture_url, "bamboo_honeypot_mui.html", "#apply")
    country = _by_label(d, "Country")
    assert (country.type, country.widget, country.required) == ("select", "hidden_select", True)
    assert country.options == ["Canada", "Mexico", "United States"]
    assert not [b for b in d.buttons if b.text == "--Select--"]     # the trigger is no button
    values = _fill(browser_page, _planned(country, "select", "United States", "United States"))
    assert values[country.n] == "United States"


# --- G7: custom single choices -------------------------------------------------------------------

def test_a_role_radio_group_is_one_question_clicked_through_its_options(browser_page,
                                                                       fixture_url):
    d = _open(browser_page, fixture_url, "rippling_generic_aria.html")
    sms = _by_label(d, "May we text you about this application?")
    assert (sms.type, sms.widget, sms.required, sms.options) == ("radio", "choice", True,
                                                                 ["Yes", "No"])
    values = _fill(browser_page, _planned(sms, "select", "No", "No"))
    assert values[sms.n] == "No"
    assert browser_page.locator("[role=radio][aria-checked=true]").inner_text() == "No"


def test_yes_no_buttons_are_one_question_and_never_buttons(browser_page, fixture_url):
    d = _open(browser_page, fixture_url, "ashby_yesno.html")
    auth = _by_label(d, "Are you legally allowed to take up employment in the United States?")
    sponsor = _by_label(d, "Will you now or in the future require visa sponsorship?")
    for f in (auth, sponsor):
        assert (f.type, f.widget, f.required, f.options) == ("radio", "choice", True,
                                                             ["Yes", "No"])
    assert [b.text for b in d.buttons] == ["Submit Application"]
    values = _fill(browser_page, _planned(auth, "select", "Yes", "Yes"),
                   _planned(sponsor, "select", "No", "No"))
    assert (values[auth.n], values[sponsor.n]) == ("Yes", "No")


def test_a_dropdown_drawn_as_a_button_is_a_field_and_its_long_list_is_matched_in_code(
        browser_page, fixture_url, tmp_path):
    d = _open(browser_page, fixture_url, "workday_create_account.html")
    browser_page.evaluate("document.getElementById('create').closest('.step').classList.remove('active');"
                          "document.getElementById('step-info').classList.add('active')")
    d = apply_form.extract(browser_page)
    country = _by_label(d, "Country")
    assert (country.type, country.widget, country.required) == ("listbox", "popup", True)
    assert _by_label(d, "How Did You Hear About Us?").required
    assert not [b for b in d.buttons if b.text == "Select One"]
    country.options = apply_fill.open_listbox_options(browser_page, country)
    assert len(country.options) == 64 and country.options.index("United States of America") > 40
    # the list is past OPTIONS_CAP: the country is found in code, no pick asked
    catalog = apply_facts.build(h.write_job_folder(tmp_path / "job"), answers=h.bank())
    plan = apply_judge.plan(d, catalog, {})
    pf = next(p for p in plan.fields if p.n == country.n)
    assert (pf.action, pf.option) == ("select", "United States of America")
    values = _fill(browser_page, pf)
    assert values[country.n] == "United States of America"


def test_a_menu_of_radio_items_behind_a_button_is_one_question(browser_page, fixture_url):
    d = _open(browser_page, fixture_url, "teamtailor_modal.html", "#decline-cookies", "#apply")
    state = _by_label(d, "Which of these states do you currently live in?")
    assert (state.type, state.widget, state.required) == ("listbox", "popup", True)
    state.options = apply_fill.open_listbox_options(browser_page, state)
    assert state.options == ["California", "New York", "Texas", "Washington"]
    # the modal stays open: no Escape went to it with the menu already closed
    assert browser_page.locator("#overlay").is_visible()
    values = _fill(browser_page, _planned(state, "select", "California", "California"))
    assert values[state.n] == "California"


def test_a_dropdown_that_opens_nothing_never_sends_an_escape_to_its_dialog(browser_page):
    # FILL-08: the Escape after reading a menu goes only to an open menu; a
    # modal form closes on any other Escape
    browser_page.set_content("""<body><div id="dlg" role="dialog" aria-modal="true"
      style="width:500px;height:300px;border:1px solid"><form>
      <label id="l">Preferred office</label>
      <button type="button" id="office" aria-haspopup="listbox" aria-labelledby="l">Select one</button>
      </form></div>
      <script>document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') document.getElementById('dlg').style.display = 'none'; });</script>
      </body>""")
    d = apply_form.extract(browser_page)
    office = _by_label(d, "Preferred office")
    assert apply_fill.open_listbox_options(browser_page, office) == []
    assert browser_page.locator("#dlg").is_visible()


def test_an_untyped_typeahead_is_typed_and_its_match_clicked(browser_page, fixture_url):
    d = _open(browser_page, fixture_url, "lever_cards.html")
    loc = _by_label(d, "Current location")
    assert (loc.type, loc.widget, loc.options) == ("listbox", "typeahead", [])
    values = _fill(browser_page, _planned(loc, "fill", "Anytown, CA"))
    assert values[loc.n] == "Anytown, CA, United States"
    assert browser_page.locator("#selected-location").input_value() == "Anytown, CA, United States"


def test_an_async_combobox_is_typed_in_and_its_match_picked(browser_page):
    # EXT-06: no option shows until the box is typed in, 300 ms later
    browser_page.set_content("""<body><form><label id="l" for="city">City *</label>
      <input id="city" role="combobox" aria-labelledby="l" aria-controls="city-list"
             aria-expanded="false" autocomplete="off">
      <div id="city-list" role="listbox"></div></form>
      <script>
        const box = document.getElementById('city'), list = document.getElementById('city-list');
        box.addEventListener('input', () => { list.innerHTML = '';
          setTimeout(() => { ['Anytown, California, United States', 'Anyville, Texas']
            .filter((c) => c.toLowerCase().startsWith(box.value.toLowerCase().slice(0, 3)))
            .forEach((c) => { const o = document.createElement('div'); o.setAttribute('role', 'option');
              o.textContent = c; o.onclick = () => { box.value = c; list.innerHTML = ''; };
              list.appendChild(o); }); }, 300); });
      </script></body>""")
    d = apply_form.extract(browser_page)
    city = _by_label(d, "City")
    assert (city.type, city.options, city.required) == ("listbox", [], True)
    values = _fill(browser_page, _planned(city, "fill", "Anytown, CA"))
    assert values[city.n] == "Anytown, California, United States"


def test_placeholder_options_are_dropped(browser_page, fixture_url):
    d = _open(browser_page, fixture_url, "lever_cards.html")
    assert _by_label(d, "How did you hear about this job?").options == [
        "LinkedIn", "Company website", "A friend"]
    browser_page.set_content("""<body><form><label for="s">Will you need sponsorship?</label>
      <select id="s"><option value="x">-- No answer --</option><option value="y">Yes</option>
      <option value="n">No</option></select></form></body>""")
    assert apply_form.extract(browser_page).fields[0].options == ["Yes", "No"]


# --- G8: a question's tick boxes -----------------------------------------------------------------

def test_a_questions_tick_boxes_are_one_field_with_its_words(browser_page, fixture_url):
    d = _open(browser_page, fixture_url, "lever_cards.html")
    langs = _by_label(d, "Which languages do you write code in?")
    assert (langs.type, langs.widget, langs.required) == ("checkbox", "checkbox_group", False)
    assert langs.options == ["Python", "SQL", "Go", "Rust"]
    assert not [f for f in d.fields if f.label in langs.options]
    values = _fill(browser_page, _planned(langs, "select", "SQL", "SQL"))
    assert values[langs.n] == "SQL"
    d = _open(browser_page, fixture_url, "greenhouse_react_select.html")
    pronouns = _by_label(d, "Which pronouns do you use?")
    assert pronouns.options == ["He/him", "She/her", "They/them"]


def test_options_that_each_carry_their_own_name_are_one_question_by_their_box(browser_page):
    # a Framer-built form (study G6, G8): every box and every radio has a
    # name of its own; the question sits in a label above the options
    hidden = "position:absolute;width:0;height:0;opacity:0"
    browser_page.set_content(f"""<body><form>
      <div><label>How would you rate your written English?</label><div>
        <div><input type="checkbox" id="e1" name="e1" style="{hidden}"><label for="e1">Basic</label></div>
        <div><input type="checkbox" id="e2" name="e2" style="{hidden}"><label for="e2">Fluent</label></div>
        <div><input type="checkbox" id="e3" name="e3" style="{hidden}"><label for="e3">Native</label></div>
      </div></div>
      <div><label>Have you worked with us before?</label><div>
        <div><input type="radio" id="wy" name="wyes" style="{hidden}"><label for="wy">Yes</label></div>
        <div><input type="radio" id="wn" name="wno" style="{hidden}"><label for="wn">No</label></div>
      </div></div></form></body>""")
    d = apply_form.extract(browser_page)
    assert [(f.label, f.type, f.options) for f in d.fields] == [
        ("How would you rate your written English?", "checkbox", ["Basic", "Fluent", "Native"]),
        ("Have you worked with us before?", "radio", ["Yes", "No"])]
    english, before = d.fields
    values = _fill(browser_page, _planned(english, "select", "Fluent", "Fluent"),
                   _planned(before, "select", "No", "No"))
    assert (values[english.n], values[before.n]) == ("Fluent", "No")
    assert browser_page.locator("#wn").is_checked() and browser_page.locator("#e2").is_checked()


# --- G12: open shadow roots ----------------------------------------------------------------------

def test_controls_inside_open_shadow_roots_are_read_located_and_filled(browser_page,
                                                                      fixture_url):
    d = _open(browser_page, fixture_url, "ukg_shadow_apply.html")
    apply = next(b for b in d.buttons if b.text == "Apply")
    assert apply.locator[1] == "#apply >> #apply-inner" and not apply.chrome
    assert [b.chrome for b in d.buttons if b.text in ("Sign In", "Find Opportunities")] == [True] * 2
    apply_fill.click(browser_page, d, apply.n, timeout_s=3)
    d = apply_form.extract(browser_page)
    first = _by_label(d, "First Name")
    assert (first.locator[1], first.required) == ("#first-name >> #inner", True)
    values = _fill(browser_page, _planned(first, "fill", "Jane"))
    assert values[first.n] == "Jane"
    assert next(b for b in d.buttons if b.text == "Submit").locator[1] == "#submit >> #btn-submit"


# --- G10, READ-10: a disabled submit is kept and flagged -----------------------------------------

def test_a_disabled_submit_is_kept_flagged_and_a_primary_one_named(browser_page, fixture_url):
    d = _open(browser_page, fixture_url, "rippling_generic_aria.html")
    apply = next(b for b in d.buttons if b.text == "Apply")
    assert (apply.disabled, apply.primary, apply.in_form) == (True, True, True)
    back = next(b for b in d.buttons if b.text == "Back to all jobs")
    assert (back.disabled, back.primary) == (False, False)


# --- EXT-07, FILL-07: options matched in code ----------------------------------------------------

@pytest.mark.parametrize("value, options, want", [
    ("United States", ["Canada", "United States of America", "Uruguay"], "United States of America"),
    ("United States", ["US", "UK"], "US"),
    ("USA", ["Canada", "United States"], "United States"),
    ("California", ["AK", "CA", "CO"], "CA"),
    ("CA", ["Arizona", "California"], "California"),
    ("Decline to self-identify", ["Male", "Female", "I don't wish to answer"],
     "I don't wish to answer"),
    ("Yes", ["Y", "N"], "Y"),
    ("California", ["California (CA)", "Colorado (CO)"], "California (CA)"),
    ("Georgia", ["Georgia", "Georgia (country)"], "Georgia"),
    ("Springfield", ["Springfield, IL", "Springfield, MA"], None),
])
def test_an_option_is_matched_in_code_through_the_names_it_goes_by(value, options, want):
    assert apply_judge.match_option(value, options) == want


def test_a_long_lists_pick_question_carries_a_shortlist_for_the_value():
    states = sorted(apply_judge._US_STATES.values())
    q = apply_judge._option_question(0, [f"Option {i}" for i in range(30)] + states, "Wisconsin")
    names = list(q["criteria"])
    assert len(names) == apply_judge.OPTIONS_CAP + 1 and "Wisconsin" in names
    assert names[0] == "Wisconsin" and names[-1] == "no_match"
    assert apply_fill._ci_match("United States", ["Canada", "USA"]) == 1


# --- EXT-10: react-select's pick is read from its sibling ----------------------------------------

def test_a_react_select_pick_reads_back_from_its_single_value(browser_page, fixture_url):
    d = _open(browser_page, fixture_url, "greenhouse_react_select.html")
    auth = _by_label(d, "Are you legally allowed to take up employment in the United States?")
    assert (auth.type, auth.required) == ("listbox", True)
    auth.options = apply_fill.open_listbox_options(browser_page, auth)
    assert auth.options == ["Yes", "No"]
    values = _fill(browser_page, _planned(auth, "select", "Yes", "Yes"))
    assert values[auth.n] == "Yes"
    assert browser_page.locator("#q_auth").input_value() == ""      # the input itself is cleared


# --- EXT-12: date parts --------------------------------------------------------------------------

def test_month_day_year_boxes_are_one_date_typed_part_by_part(browser_page, fixture_url):
    browser_page.set_viewport_size({"width": 1400, "height": 1000})
    browser_page.goto(fixture_url("workday_create_account.html"))
    browser_page.evaluate("document.querySelectorAll('.step').forEach(s => s.classList.remove('active'));"
                          "document.getElementById('step-disclosure').classList.add('active')")
    d = apply_form.extract(browser_page)
    date = _by_label(d, "Today's Date")
    assert (date.type, date.widget, date.required) == ("date", "date:MDY", True)
    assert len(date.option_locators) == 3
    values = _fill(browser_page, _planned(date, "fill", "2026-09-24"))
    assert values[date.n] == "09/24/2026"


# --- EXT-13: a rich-text box ---------------------------------------------------------------------

def test_a_rich_text_box_is_a_field_filled_as_typing_would(browser_page):
    browser_page.set_content("""<body><form><div id="lbl">Cover letter *</div>
      <div id="ed" contenteditable="true" aria-labelledby="lbl" style="min-height:60px;border:1px solid"></div>
      </form></body>""")
    d = apply_form.extract(browser_page)
    box = _by_label(d, "Cover letter")
    assert (box.type, box.widget, box.required) == ("textarea", "editable", True)
    values = _fill(browser_page, _planned(box, "fill", "I am writing to apply."))
    assert values[box.n] == "I am writing to apply."


# --- EXT-19, FILL-02: stable locators and the control's identity ---------------------------------

def test_a_stable_attribute_locates_a_control_and_a_moved_one_is_never_typed_into(
        browser_page):
    browser_page.set_content("""<body><form id="f">
      <div class="row"><label>City <input data-automation-id="addressCity"></label></div>
      <div class="row"><label>Postal code <input></label></div></form></body>""")
    d = apply_form.extract(browser_page)
    city = _by_label(d, "City")
    postal = _by_label(d, "Postal code")
    assert city.locator[1] == 'input[data-automation-id="addressCity"]'
    assert postal.locator[1].startswith("body > ")
    # a question inserted above shifts the postal box's path onto another box
    browser_page.evaluate("""() => { const row = document.createElement('div');
      row.className = 'row';
      row.innerHTML = '<label>Nickname <input name="nick"></label>';
      document.getElementById('f').prepend(row); }""")
    out = apply_fill.apply(browser_page, FillPlan(fields=[_planned(postal, "fill", "12345")]))
    # the box at the old path is another one now: the value goes to the box
    # with the planned identity, never into the nickname
    assert browser_page.locator("input[name=nick]").input_value() == ""
    assert out[0].value == "12345"
    assert browser_page.locator("input:not([name]):not([data-automation-id])").input_value() \
        == "12345"


# --- FILL-13: picks are verified in code ---------------------------------------------------------

@pytest.mark.parametrize("value, option, group, ok", [
    ("checked", "checked", False, True), ("", "checked", False, False),
    ("Yes", "Yes", False, True), ("No", "Yes", False, False),
    ("United States of America", "United States", False, True),
    ("SQL, Go", "Go", True, True), ("SQL, Go", "Python", True, False),
    ("California", "CA", False, True), ("Select One", "Canada", False, False),
    # review M3: words that only contain the option are no pick of it
    ("Yes, but I will need sponsorship", "Yes", False, False),
    ("SQL, Go", "Go", False, False)])
def test_a_pick_is_verified_in_code_against_the_read_back(value, option, group, ok):
    assert apply_run.pick_holds(value, option, group) is ok


# --- READ-05: the section headings go with the fields --------------------------------------------

def test_the_mapping_sends_the_section_headings_with_the_fields(browser_page, fixture_url,
                                                               tmp_path):
    d = _open(browser_page, fixture_url, "lever_cards.html")
    catalog = apply_facts.build(h.write_job_folder(tmp_path / "job"), answers=h.bank())
    state, q = apply_judge.page_questions(d, catalog, {})
    assert state["sections"] == [{"heading": "Submit your application", "fields": [0, 1, 2, 3]},
                                 {"heading": "A few more questions", "fields": [4, 5, 6]}]
    # the fake reads a field's question alone: the sections leave its answers as they were
    bare = {k: v for k, v in state.items() if k != "sections"}
    assert {k: a.choice for k, a in jev.FakeJev().judge(state, q).items()} == \
        {k: a.choice for k, a in jev.FakeJev().judge(bare, q).items()}
    plan = apply_judge.plan(d, catalog, {})
    s2, _ = apply_judge.reask_questions(d, catalog, plan, [4], what="source")
    assert s2["sections"] == [{"heading": "A few more questions", "fields": [4]}]
    json.dumps(state)
