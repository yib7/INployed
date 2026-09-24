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
      <label for="site">Website</label><input id="site" type="text" data-harness-junk="honeypot">
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


def test_a_moved_box_with_no_attributes_is_told_apart_by_its_label(browser_page):
    # review I5: boxes that carry no id, name or test attribute are told apart
    # by their label's words; the inserted box has no attribute either
    browser_page.set_content("""<body><form id="f">
      <div class="row"><label>City <input></label></div>
      <div class="row"><label>Postal code <input></label></div></form></body>""")
    d = apply_form.extract(browser_page)
    city, postal = _by_label(d, "City"), _by_label(d, "Postal code")
    assert city.ident != postal.ident
    assert postal.ident.endswith("|postal code")
    browser_page.evaluate("""() => { const row = document.createElement('div');
      row.className = 'row';
      row.innerHTML = '<label>Nickname <input></label>';
      document.getElementById('f').prepend(row); }""")
    out = apply_fill.apply(browser_page, FillPlan(fields=[_planned(postal, "fill", "12345")]))
    values = browser_page.locator("input").evaluate_all("els => els.map((e) => e.value)")
    assert values == ["", "", "12345"], values          # Nickname, City, Postal code
    assert out[0].value == "12345"
    # one definition of the identity: the extractor's and the filler's agree
    live = browser_page.locator("input").nth(2).evaluate(apply_form.IDENT_FN_JS)
    assert live == postal.ident
    assert apply_form.same_ident(postal.ident, postal.ident + " required")
    assert not apply_form.same_ident(postal.ident, city.ident)


# --- FILL-13: picks are verified in code ---------------------------------------------------------

@pytest.mark.parametrize("value, option, group, ok", [
    ("checked", "checked", False, True), ("", "checked", False, False),
    ("Yes", "Yes", False, True), ("No", "Yes", False, False),
    ("United States of America", "United States", False, True),
    ("SQL, Go", "Go", True, True), ("SQL, Go", "Python", True, False),
    ("California", "CA", False, True), ("Select One", "Canada", False, False),
    # review M3: words that only contain the option are no pick of it
    ("Yes, but I will need sponsorship", "Yes", False, False),
    ("SQL, Go", "Go", False, False),
    # review R2 Minor 4: a read-back shorter than the planned option neither
    ("Yes", "Yes, I will need sponsorship", False, False),
    ("No", "No, not at this time", False, False)])
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


# === SP5 review round 1 ============================================================================

# --- I2: a required marker drawn by CSS or named by a class --------------------------------------

def test_a_star_drawn_by_css_or_a_required_class_marks_the_question_required(browser_page):
    browser_page.set_content("""<head><style>
      .q-title::after { content: "*"; color: #b00; }
      .opt-title::after { content: " (optional)"; }
      </style></head><body><form>
      <div><label class="q-title" for="a">Preferred start date</label><input id="a"></div>
      <div><label class="field-required" for="b">Current employer</label><input id="b"></div>
      <div><label class="not-required" for="c">Nickname</label><input id="c"></div>
      <div><label class="opt-title" for="d">Middle name</label><input id="d"></div>
      <fieldset><label class="q-title">Acknowledgement</label>
        <div><input type="checkbox" id="e"><label for="e">I have read the policy.</label></div>
      </fieldset></form></body>""")
    d = apply_form.extract(browser_page)
    assert [(f.label, f.required) for f in d.fields] == [
        ("Preferred start date", True), ("Current employer", True), ("Nickname", False),
        ("Middle name", False), ("I have read the policy.", True)]


# --- I3: an upload hidden until "Attach resume" is clicked ----------------------------------------

def test_a_file_box_hidden_under_its_shown_question_is_kept_and_a_hidden_steps_is_not(
        browser_page, fixture_url):
    d = _open(browser_page, fixture_url, "aria_controls.html")
    resume = _by_label(d, "Resume")
    assert (resume.type, resume.required) == ("file", True)
    browser_page.set_content("""<body><form>
      <div class="step"><label for="n">Name *</label><input id="n" required></div>
      <div class="step" style="display:none"><label for="cv">Resume *</label>
        <input type="file" id="cv" required></div></form></body>""")
    assert [f.label for f in apply_form.extract(browser_page).fields] == ["Name"]


# --- I4: a link inside a label keeps its words ------------------------------------------------------

def test_a_link_that_opens_a_dialog_keeps_its_words_in_the_label(browser_page, fixture_url):
    d = _open(browser_page, fixture_url, "oracle_email_terms.html")
    assert [f.label for f in d.fields][1] == ("I agree with the terms and conditions of the "
                                              "application")
    # a dropdown's shown value inside a label still stays out of it
    browser_page.set_content("""<body><form><label for="s">State
      <div aria-haspopup="listbox"><span>Select a state</span><input id="s" type="text"></div>
      </label></form></body>""")
    assert [f.label for f in apply_form.extract(browser_page).fields] == ["State"]


# --- I6: a menu that closed under the option's click is opened once more -------------------------

class _Options:
    def __init__(self, texts, fail=False):
        self.texts = texts
        self.fail = fail
        self.clicks: list[int] = []

    def all_inner_texts(self):
        return list(self.texts)

    def nth(self, i):
        outer = self

        class _One:
            def click(self, timeout=None):
                outer.clicks.append(i)
                if outer.fail:
                    raise TimeoutError("the menu closed under the click")
        return _One()


def test_an_option_click_that_fails_opens_the_menu_once_more_and_clicks_again(monkeypatch):
    first, second = _Options(["Remote", "Hybrid"], fail=True), _Options(["Remote", "Hybrid"])
    menus = [first, second]
    opened = []

    def _open_menu(frame, loc, *, popup=False, face=None):
        opened.append(popup)
        return menus.pop(0) if menus else None
    monkeypatch.setattr(apply_fill, "_open_menu", _open_menu)
    apply_fill._pick_listbox(None, None, None, "Hybrid")
    assert opened == [False, False]
    assert (first.clicks, second.clicks) == ([1], [1])
    # a second failure is the pick's failure
    menus[:] = [_Options(["Remote"], fail=True), _Options(["Remote"], fail=True)]
    with pytest.raises(TimeoutError):
        apply_fill._pick_listbox(None, None, None, "Remote")


# --- I7: a custom tick box (role=checkbox, role=switch) ---------------------------------------------

def test_a_custom_tick_box_is_a_field_ticked_and_read_back(browser_page, fixture_url):
    d = _open(browser_page, fixture_url, "aria_controls.html")
    certify = _by_label(d, "I certify that the information in this application is accurate")
    texts = _by_label(d, "Send me updates about this application by text message")
    assert (certify.type, certify.widget, certify.required, certify.options) == (
        "checkbox", "aria_check", True, ["checked"])
    assert (texts.widget, texts.required) == ("aria_check", False)
    values = _fill(browser_page, _planned(certify, "select", "yes", "checked"),
                   _planned(texts, "select", "no", "unchecked"))
    assert values == {certify.n: "checked", texts.n: ""}
    assert browser_page.locator("#certify").get_attribute("aria-checked") == "true"
    # ticked already: left as it is
    values = _fill(browser_page, _planned(certify, "select", "yes", "checked"))
    assert browser_page.locator("#certify").get_attribute("aria-checked") == "true"


# --- I8 (EXT-16, G3): a posting's widget groups -------------------------------------------------

def test_a_postings_filters_search_box_alert_form_and_footer_picker_are_no_fields(browser_page):
    browser_page.set_content("""<body><h1>Open roles</h1>
      <div id="open-roles-filters"><div><div><label for="dep">Department</label></div>
        <button id="dep" type="button" aria-haspopup="listbox">All departments</button></div>
        <div><div><div><label for="q">Search</label></div><div><div>
          <input id="q" placeholder="Search roles"></div></div></div></div></div>
      <a class="btn" href="/apply">Apply now</a>
      <aside><form><label for="e">Email</label><input id="e" type="email">
        <button type="submit">Notify me</button></form></aside>
      <div><div><a href="/terms">Terms of service</a><a href="/privacy">Privacy</a>
        <a href="https://example.com">Powered by Example</a></div>
        <div><div><input role="combobox" aria-label="Search" aria-haspopup="listbox"
          value="United States (English)"></div></div></div></body>""")
    assert apply_form.extract(browser_page).fields == []


def test_an_application_forms_boxes_stay_beside_its_search_labelled_dropdown(browser_page):
    browser_page.set_content("""<body><div class="application">
      <div><span>Location *</span><input role="combobox" aria-label="Search" required></div>
      <div><label for="f">First name</label><input id="f"></div>
      <p>By applying you accept our <a href="/terms">Terms of service</a> and
        <a href="/privacy">Privacy</a>.</p>
      <button type="button">Submit application</button></div></body>""")
    assert [f.label for f in apply_form.extract(browser_page).fields] == ["Location", "First name"]


# --- M4: honeypot words as a whole label --------------------------------------------------------

def test_a_question_that_says_leave_blank_or_do_not_enter_is_no_honeypot(browser_page):
    browser_page.set_content("""<body><form>
      <label for="p">Phone number (do not enter dashes or spaces) *</label><input id="p" required>
      <label for="m">Middle name (leave this field blank if none)</label><input id="m">
      <label for="h">Please leave this field blank</label><input id="h">
      <label for="r">Website (for robots only)</label><input id="r"></form></body>""")
    assert [f.label for f in apply_form.extract(browser_page).fields] == [
        "Phone number (do not enter dashes or spaces)",
        "Middle name (leave this field blank if none)"]


# --- M5: statements under a line that asks nothing stay apart -----------------------------------

def test_two_consent_statements_under_a_heading_line_stay_two_boxes(browser_page):
    browser_page.set_content("""<body><form><div><p>Consent:</p>
      <label><input type="checkbox" name="c1" required> I agree to the terms</label>
      <label><input type="checkbox" name="c2"> I agree to receive texts</label></div>
      </form></body>""")
    d = apply_form.extract(browser_page)
    assert [(f.label, f.widget, f.options) for f in d.fields] == [
        ("I agree to the terms", "", ["checked"]), ("I agree to receive texts", "", ["checked"])]


# --- M6: a read-only picker stays a field ---------------------------------------------------------

def test_a_read_only_picker_stays_a_field_and_a_read_only_box_does_not(browser_page):
    browser_page.set_content("""<body><form>
      <label for="g">Gender</label><input id="g" role="combobox" readonly aria-expanded="false">
      <label for="d">Start date</label><input id="d" class="react-datepicker__input" readonly>
      <label for="s">Link to this job</label><input id="s" readonly value="https://x.example/1">
      </form></body>""")
    assert [f.label for f in apply_form.extract(browser_page).fields] == ["Gender", "Start date"]


# --- M7: a heading counts only when its own box holds the control ---------------------------------

def test_a_section_heading_is_one_whose_box_holds_the_control(browser_page, fixture_url):
    browser_page.set_content("""<body><h2>Compensation</h2><p>Base pay range.</p>
      <div class="autofill"><h3>Autofill from resume</h3><button>Upload file</button></div>
      <form><div><label for="n">Name</label><input id="n"></div>
      <h3>Eligibility</h3><div><label for="w">Work authorization</label><input id="w"></div>
      </form></body>""")
    d = apply_form.extract(browser_page)
    assert [(f.label, f.section) for f in d.fields] == [("Name", ""),
                                                        ("Work authorization", "Eligibility")]


# --- M8: a question's tick boxes take every chosen option ---------------------------------------

def test_every_option_the_value_names_is_ticked(browser_page, fixture_url):
    d = _open(browser_page, fixture_url, "lever_cards.html")
    langs = _by_label(d, "Which languages do you write code in?")
    values = _fill(browser_page, _planned(langs, "select", "Python, SQL and Rust", "Python"))
    assert values[langs.n] == "Python, SQL, Rust"


@pytest.mark.parametrize("value, option, chosen", [
    # review R2 Minor 3: a value that is one option's whole name is that option
    ("Research and Development", "Research and Development", ["Research and Development"]),
    ("Sales, Marketing", "Sales", ["Sales", "Marketing"]),
    ("Research, Development", "Research", ["Research", "Development"]),
])
def test_a_value_that_names_one_option_whole_ticks_that_option_alone(value, option, chosen):
    options = ["Research", "Development", "Research and Development", "Sales", "Marketing"]
    pf = PlannedField(n=0, locator=(0, "#g"), label="Which teams?", required=False,
                      fact_key="teams", value=value, option=option, confidence=0.9,
                      action="select", widget="checkbox_group", options=options,
                      option_locators=[f"#o{i}" for i in range(len(options))])
    assert [options[i] for i in apply_fill._chosen(pf, option)] == chosen


# --- M14: a choice never clicks a form's submit control -----------------------------------------

def test_a_choice_option_that_is_a_forms_submit_is_never_clicked(browser_page):
    browser_page.set_content("""<body><form onsubmit="event.preventDefault();
        document.body.dataset.sent = 1"><p>Relocate?</p>
      <div><button aria-pressed="false">Yes</button><button aria-pressed="false">No</button></div>
      <input name="n"></form></body>""")
    d = apply_form.extract(browser_page)
    q = next(f for f in d.fields if f.widget == "choice")
    errors: list = []
    apply_fill.apply(browser_page, FillPlan(fields=[_planned(q, "select", "Yes", "Yes")]),
                     errors=errors)
    assert [e["error"] for e in errors] == ["LookupError"]
    assert browser_page.evaluate("document.body.dataset.sent") is None


# --- review M1: Workday's click filter over its real button --------------------------------------

def test_a_button_under_a_click_filter_of_the_same_words_is_one_button(browser_page, fixture_url):
    d = _open(browser_page, fixture_url, "workday_create_account.html")
    assert [b.text for b in d.buttons if not b.chrome] == [
        "Create Account", "Sign In", "Back to Job Posting"]
    create = next(b for b in d.buttons if b.text == "Create Account")
    assert create.locator[1] == "#create-filter"


# === SP5 review round 2 ============================================================================

# --- R2-I1: a hidden required mark, and a fieldset's title --------------------------------------

def test_a_hidden_required_mark_and_another_questions_label_mark_nothing_required(browser_page):
    browser_page.set_content("""<head><style>.ng-hide { display: none !important; }</style>
      </head><body><form>
      <div><label for="s">Experience Summary <span class="ng-hide required">*</span></label>
        <textarea id="s"></textarea></div>
      <div><label for="n">Full Name <span class="required">*</span></label><input id="n"></div>
      <fieldset><label for="fn">First name *</label><input id="fn">
        <label for="mn">Middle name</label><input id="mn">
        <label for="nk">Nickname</label><input id="nk"></fieldset>
      <fieldset><legend>Contact <span class="required">*</span></legend>
        <label for="ph">Phone</label><input id="ph"></fieldset>
      </form></body>""")
    d = apply_form.extract(browser_page)
    assert [(f.label, f.required) for f in d.fields] == [
        ("Experience Summary", False), ("Full Name", True), ("First name", True),
        ("Middle name", False), ("Nickname", False), ("Phone", True)]


# --- R2-I2: only a search, alert or subscribe group is a posting's widget -------------------------

def test_a_clear_button_a_prefixed_search_box_an_outside_next_and_an_eeo_note_keep_their_fields(
        browser_page):
    browser_page.set_content("""<body><div class="app">
      <div class="q"><div><label for="p">Phone *</label><input id="p" required></div>
        <button type="button">Clear</button></div>
      <div class="location-search-box"><label for="loc">Location *</label>
        <input id="loc" required aria-autocomplete="list">
        <ul role="listbox" hidden><li role="option">Anytown</li></ul></div>
      <div class="eeo"><div><label for="g">Gender</label>
        <select id="g"><option value="">Select</option><option>Female</option>
          <option>Male</option></select></div>
        <p>See our <a href="/privacy">Privacy Policy</a> and
          <a href="/terms">Terms of Service</a>.</p></div>
      <form id="details"><label for="e">Email *</label><input id="e" type="email" required>
        <button type="button">Clear</button></form>
      <form id="more"><label for="c">City</label><input id="c"></form>
      <form id="step"><label for="t">Team</label><input id="t">
        <button type="button">Search</button></form>
      <div class="footer"><button type="submit" form="details">Next</button>
        <button type="submit" form="step">Save and continue</button></div>
      </div></body>""")
    assert [f.label for f in apply_form.extract(browser_page).fields] == [
        "Phone", "Location", "Gender", "Email", "City", "Team"]


def test_a_form_whose_buttons_sit_outside_it_is_read_by_the_pages_buttons(browser_page):
    browser_page.set_content("""<body><h1>Open roles</h1>
      <form id="find"><label for="c">City</label><input id="c"></form>
      <div class="bar"><button type="button">Search jobs</button>
        <button type="button">Clear</button></div></body>""")
    assert apply_form.extract(browser_page).fields == []


def test_a_search_and_clear_group_is_still_a_postings_widget(browser_page):
    browser_page.set_content("""<body><h1>Open roles</h1>
      <div class="search-bar"><label for="kw">Team</label><input id="kw">
        <button type="button">Search</button><button type="button">Clear</button></div>
      <form><label for="loc">Office</label><input id="loc">
        <button type="submit">Search</button><button type="reset">Reset</button></form>
      <a class="btn" href="/apply">Apply now</a></body>""")
    assert apply_form.extract(browser_page).fields == []


# --- R2-I3: a section heading in its own header box ------------------------------------------------

def test_a_heading_in_its_own_header_box_is_the_section_and_a_parsers_box_is_not(browser_page):
    # Ashby's shape: the posting's details in a left column beside the form,
    # the parser's upload before its own heading; Greenhouse's header box
    browser_page.set_content("""<body><main><h1>Analytics Engineer</h1>
      <div class="details" style="display: flex">
      <div class="left" style="width: 300px"><h2>Compensation</h2><p>Base pay range.</p></div>
      <div id="application" style="width: 700px">
        <div class="autofill"><input type="file" id="parse" aria-label="Upload file">
          <div class="header"><h3>Autofill from resume</h3>
          <p>Upload your resume here to fill the fields below.</p></div></div>
        <form><div class="field"><label for="fn">First Name</label><input id="fn"></div>
          <div class="section-header"><h3>Voluntary Self-Identification</h3>
            <p>Completion is voluntary.</p></div>
          <div class="field"><label for="g">Gender</label>
            <select id="g"><option>Female</option><option>Male</option></select></div>
          <div class="field"><label for="r">Race</label>
            <select id="r"><option>Asian</option><option>White</option></select></div>
        </form></div></div></main></body>""")
    d = apply_form.extract(browser_page)
    got = {f.type if f.type == "file" else f.label: f.section for f in d.fields}
    # a heading in another column is no section; the parser's box (its own
    # upload and its own heading) closes off the headings before it; the
    # header box beside the fields is their section
    assert got == {"file": "", "First Name": "",
                   "Gender": "Voluntary Self-Identification",
                   "Race": "Voluntary Self-Identification"}


# --- R2 Minor 1: "date" as a whole word only --------------------------------------------------

def test_a_read_only_box_named_candidate_or_update_is_no_date_picker(browser_page):
    browser_page.set_content("""<body><form>
      <label for="ce">Email</label><input id="ce" name="candidate_email" readonly value="a@b.c">
      <label for="ul">Link</label><input id="ul" name="update_link" readonly value="https://x">
      <label for="sd">Start</label><input id="sd" name="start_date" readonly>
      <label for="ed">End</label><input id="ed" name="endDate" readonly>
      <label for="dp">When</label><input id="dp" class="datepicker-input" readonly>
      </form></body>""")
    assert [f.label for f in apply_form.extract(browser_page).fields] == ["Start", "End", "When"]


# --- R2 Minor 2: react-select without search --------------------------------------------------

_REACT_SELECT_DUMMY = """<body><form>
  <label id="g-label" for="g-input">Gender *</label>
  <div class="select__control" id="g-face"
       style="border: 1px solid #888; width: 260px; height: 36px; display: grid">
    <div class="select__value-container" style="grid-area: 1 / 1 / 2 / 3; padding: 6px">
      <div class="select__placeholder" id="g-shown">Select...</div>
      <input id="g-input" role="combobox" readonly aria-readonly="true" aria-autocomplete="list"
        aria-expanded="false" aria-haspopup="true" aria-labelledby="g-label" inputmode="none"
        tabindex="0" value=""
        style="background: 0; border: 0; caret-color: transparent; font-size: inherit;
               outline: 0; padding: 0; width: 1px; color: transparent; left: -100px;
               opacity: 0; position: relative; transform: scale(.01)">
    </div></div>
  <button type="submit">Submit application</button></form>
<script>
  var face = document.getElementById('g-face'), input = document.getElementById('g-input');
  var menu = null;
  face.addEventListener('mousedown', function (e) {
    e.preventDefault();
    if (menu) return;
    menu = document.createElement('div');
    menu.setAttribute('role', 'listbox');
    menu.id = 'g-listbox';
    ['Female', 'Male', 'Decline to self-identify'].forEach(function (t) {
      var o = document.createElement('div');
      o.setAttribute('role', 'option');
      o.textContent = t;
      o.addEventListener('click', function () {
        var shown = document.getElementById('g-shown');
        shown.className = 'select__single-value';
        shown.textContent = t;
        menu.remove(); menu = null;
        input.setAttribute('aria-expanded', 'false');
        input.removeAttribute('aria-controls');
      });
      menu.appendChild(o);
    });
    document.body.appendChild(menu);
    input.setAttribute('aria-controls', 'g-listbox');
    input.setAttribute('aria-expanded', 'true');
  });
</script></body>"""


def test_react_selects_dummy_input_is_a_dropdown_opened_through_its_face(browser_page):
    browser_page.set_content(_REACT_SELECT_DUMMY)
    d = apply_form.extract(browser_page)
    gender = _by_label(d, "Gender")
    assert (gender.type, gender.required) == ("listbox", True)
    # the box a person clicks, the one that holds the dummy input
    assert gender.click_locator
    assert browser_page.locator(gender.click_locator[1]).locator("#g-input").count() == 1
    values = _fill(browser_page, _planned(gender, "select", "Female", "Female"))
    assert values[gender.n] == "Female"
    assert browser_page.inner_text("#g-shown") == "Female"


# --- a popup button that names a menu or a step is no dropdown field (fix round 3) ---------------

def test_a_popup_button_that_says_more_menu_apply_next_or_back_is_no_field(browser_page):
    # SP5's extractor carried backspace bytes where `\b` belonged, so these
    # words never matched (found in fix round 3)
    browser_page.set_content("""<body><form>
      <div><span id="src-l">How did you hear about us?</span>
        <button type="button" aria-haspopup="listbox" aria-labelledby="src-l">Select One</button></div>
      <div><button type="button" aria-haspopup="menu">More</button>
        <button type="button" aria-haspopup="true">Menu</button>
        <button type="button" aria-haspopup="true">Apply</button>
        <button type="button" aria-haspopup="true">Next</button>
        <button type="button" aria-haspopup="true">Back</button></div>
      <div class="upload"><input type="file" id="cv" style="display:none">
        <button type="button">CV</button></div>
      </form></body>""")
    d = apply_form.extract(browser_page)
    assert [(f.label, f.widget or f.type) for f in d.fields] == [
        ("How did you hear about us?", "popup"), ("CV", "file")]


# === SP5 review round 3 ============================================================================

# --- R3-I1: a consent's floor from the label the extractor read -----------------------------------

_LONG_ROUTINE = ("I have read the privacy notice for candidates and I understand how my personal "
                 "information is collected, processed, stored and retained for this application, "
                 "and I certify that the information provided in this application is true, "
                 "accurate and complete to the best of my knowledge, and I agree to the terms "
                 "and conditions of this application.")


@pytest.mark.parametrize("box, partial, ticked", [
    # the button's words are left out of the label: read in part
    ("""<label><input type="checkbox" id="c" required> I agree to the Privacy Policy and the
        <button type="button">Background Check Disclosure</button></label>""", True, False),
    # a question box's words past the cap: read in part
    (f"""<div class="q"><p>{_LONG_ROUTINE} I consent to a criminal background check</p>
        <input type="checkbox" id="c" required></div>""", True, False),
    ("""<label><input type="checkbox" id="c" required>
        Я согласен на проверку судимости и Privacy Policy</label>""", False, False),
    ("""<label><input type="checkbox" id="c" required> 我同意背景调查 Privacy Policy</label>""",
     False, False),
    ("""<label><input type="checkbox" id="c" required>
        I consent to the Biometric Privacy Policy</label>""", False, False),
    # an optional box
    ("""<label><input type="checkbox" id="c">
        I agree to the privacy notice for candidates</label>""", False, False),
    ("""<label><input type="checkbox" id="c"> I accept the Talent Network Terms</label>""",
     False, False),
    # the one that ticks: required, whole, routine
    ("""<label><input type="checkbox" id="c" required>
        I agree to the privacy notice for candidates</label>""", False, True),
])
def test_a_consent_read_in_part_in_another_script_named_or_optional_keeps_its_high_floor(
        browser_page, tmp_path, box, partial, ticked):
    browser_page.set_content(f"<body><form>{box}</form></body>")
    d = apply_form.extract(browser_page)
    assert len(d.fields) == 1, [(f.label, f.type) for f in d.fields]
    f = d.fields[0]
    assert f.label_partial is partial, f.label
    catalog = apply_facts.build(h.write_job_folder(tmp_path / "job"), answers=h.bank())
    answers = {"field_0_source": jev.Answer(kind="choice", choice="consent_attest",
                                            probabilities={"consent_attest": 0.74},
                                            confidence=0.74)}
    plan = apply_judge.plan(d, catalog, answers, company="Fabrikam")
    assert (plan.fields[0].action == "select") is ticked, f.label
    # the flag rides the digest's JSON
    assert apply_form.FormDigest.from_dict(d.to_dict()).fields[0].label_partial is partial


# === SP5 review round 4 ============================================================================

# --- R4-I1: a chrome word drops a popup only as its whole text ------------------------------------

def test_a_popup_question_whose_words_hold_apply_next_more_or_back_is_a_field(browser_page):
    # the words round 3's backspace bytes kept inert, live since 9a6c42c
    browser_page.set_content("""<body><form>
      <div><label for="m1">Which of these apply to you?</label>
        <button type="button" id="m1" aria-haspopup="menu">Select...</button></div>
      <div><label for="m2">Select all that apply</label>
        <button type="button" id="m2" aria-haspopup="menu">Choose</button></div>
      <div><label for="m3">Can you start within the next 30 days?</label>
        <button type="button" id="m3" aria-haspopup="true">Select...</button></div>
      <div><label for="m4">Tell us more about your availability</label>
        <button type="button" id="m4" aria-haspopup="menu">Select...</button></div>
      <div><label for="l1">Team</label>
        <button type="button" id="l1" aria-haspopup="listbox">Back end</button></div>
      <div><label for="l2">Experience</label>
        <button type="button" id="l2" aria-haspopup="listbox">More than 5 years</button></div>
      <div><label for="l3">Veteran status</label>
        <button type="button" id="l3" aria-haspopup="listbox">Does not apply</button></div>
      <div><label for="m5">Which language do you prefer for interviews?</label>
        <button type="button" id="m5" aria-haspopup="menu">Select...</button></div>
      </form></body>""")
    d = apply_form.extract(browser_page)
    assert [(f.label, f.widget) for f in d.fields] == [
        ("Which of these apply to you?", "popup"), ("Select all that apply", "popup"),
        ("Can you start within the next 30 days?", "popup"),
        ("Tell us more about your availability", "popup"), ("Team", "popup"),
        ("Experience", "popup"), ("Veteran status", "popup"),
        # a label that asks is a question, a chrome word in it or not
        ("Which language do you prefer for interviews?", "popup")]


def test_a_named_chrome_popup_is_still_no_field(browser_page):
    # the chrome controls a page draws as popups: their whole text (or their
    # label) is a chrome word, or their label names an account, a language,
    # a share, a sort or a filter
    browser_page.set_content("""<body><form>
      <button type="button" aria-haspopup="menu" aria-label="More">More</button>
      <button type="button" aria-haspopup="true" aria-label="Menu">Menu</button>
      <button type="button" aria-haspopup="listbox">Apply</button>
      <button type="button" aria-haspopup="listbox">Next</button>
      <button type="button" aria-haspopup="listbox">Back</button>
      <button type="button" aria-haspopup="menu" aria-label="Share this job">Share</button>
      <button type="button" aria-haspopup="listbox">Sort by: Newest</button>
      <div><label for="lang">Language</label>
        <button type="button" id="lang" aria-haspopup="menu">English</button></div>
      <div><label for="acct">Your account</label>
        <button type="button" id="acct" aria-haspopup="true">Jane</button></div>
      </form></body>""")
    assert apply_form.extract(browser_page).fields == []


# --- R4 Minor 1: every skipped subtree with words marks the label read in part --------------------

@pytest.mark.parametrize("inner, partial", [
    ('I agree to the Privacy Policy <span aria-hidden="true">and consent to a background '
     'check</span>', True),
    ('I agree to the Privacy Policy <span role="tooltip">and consent to a background '
     'check</span>', True),
    # a star is a marker, never words left out
    ('I agree to the privacy notice for candidates <span aria-hidden="true">*</span>', False),
])
def test_a_labels_skipped_words_mark_it_read_in_part(browser_page, inner, partial):
    browser_page.set_content(f"""<body><form><label><input type="checkbox" id="c" required>
      {inner}</label></form></body>""")
    f = apply_form.extract(browser_page).fields[0]
    assert f.label_partial is partial, f.label
