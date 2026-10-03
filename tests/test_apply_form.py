"""`apply_form.extract` over the local HTML fixtures (headless Chromium).

Every test drives a real page served by `conftest_browser.fixtures_server`;
nothing here reaches the network. The module skips when Playwright is not
installed and the fixtures skip when Chromium is missing.
"""
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("playwright")

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import apply_form  # noqa: E402
import apply_judge  # noqa: E402

pytest_plugins = ["conftest_browser"]


def _by_label(digest, label):
    hits = [f for f in digest.fields if f.label == label]
    assert hits, f"no field labelled {label!r}; have {[f.label for f in digest.fields]}"
    return hits[0]


def _by_id(digest, id_or_name):
    hits = [f for f in digest.fields if f.id_or_name == id_or_name]
    assert hits, f"no field {id_or_name!r}; have {[f.id_or_name for f in digest.fields]}"
    return hits[0]


# --- (a) the Greenhouse embed: fields live in the iframe ----------------------------

@pytest.fixture
def greenhouse(browser_page, fixture_url):
    browser_page.goto(fixture_url("greenhouse_embed.html"))
    browser_page.frame_locator("#grnhse_iframe").locator("#first_name").wait_for()
    return apply_form.extract(browser_page)


def test_greenhouse_iframe_fields_carry_frame_index_one(greenhouse):
    first = _by_id(greenhouse, "first_name")
    assert first.locator == (1, "#first_name")
    assert first.type == "text"
    assert first.label == "First Name"
    assert first.required is True
    email = _by_id(greenhouse, "email")
    assert email.type == "email" and email.locator[0] == 1
    resume = _by_id(greenhouse, "resume")
    assert resume.type == "file" and resume.required is True
    assert resume.locator == (1, "#resume")


def test_greenhouse_required_star_is_stripped_from_the_label(greenhouse):
    last = _by_id(greenhouse, "last_name")
    assert last.label == "Last Name" and last.required
    phone = _by_id(greenhouse, "phone")
    assert phone.required is False and phone.placeholder == "+1 555 555 0100"


def test_greenhouse_submit_button_has_kind_hint_submit(greenhouse):
    submit = [b for b in greenhouse.buttons if b.text == "Submit application"]
    assert len(submit) == 1
    assert submit[0].kind_hint == "submit"
    assert submit[0].locator == (1, "#submit_app")


def test_greenhouse_select_options_skip_the_placeholder(greenhouse):
    hear = _by_label(greenhouse, "How did you hear about us?")
    assert hear.type == "select"
    assert hear.options == ["LinkedIn", "Company website", "Referral", "Other"]
    gender = _by_label(greenhouse, "Gender")
    assert "Decline to self-identify" in gender.options


def test_greenhouse_radio_group_is_one_field_with_legend_label(greenhouse):
    auth = _by_label(greenhouse, "Are you legally authorized to work in the United States?")
    assert auth.type == "radio"
    assert auth.options == ["Yes", "No"]
    assert auth.required is True
    assert auth.id_or_name == "work_auth"
    assert sum(1 for f in greenhouse.fields if f.type == "radio") == 1


def test_greenhouse_checkbox_is_one_field_with_checked_option(greenhouse):
    cert = _by_id(greenhouse, "certify")
    assert cert.type == "checkbox"
    assert cert.options == ["checked"]
    assert cert.label == "I certify that the information provided is accurate"
    assert cert.required is True


def test_greenhouse_hidden_and_disabled_inputs_are_skipped(greenhouse):
    ids = {f.id_or_name for f in greenhouse.fields}
    assert "job_id" not in ids            # type=hidden
    assert "website" not in ids           # display:none honeypot
    assert "salary_old" not in ids        # disabled


def test_greenhouse_help_text_and_url_type(greenhouse):
    linkedin = _by_label(greenhouse, "LinkedIn Profile")
    assert linkedin.type == "url"
    assert linkedin.help == "Paste the full URL of your profile."


def test_greenhouse_text_title_and_host(greenhouse):
    assert greenhouse.url_host == "127.0.0.1"
    assert greenhouse.title == "Data Analyst at Acme Analytics"
    assert "Apply for this job" in greenhouse.text          # main frame
    assert "Voluntary Self-Identification" in greenhouse.text   # iframe
    assert len(greenhouse.text) <= apply_judge.PAGE_TEXT_CAP


def test_field_numbers_run_across_frames_in_document_order(greenhouse):
    assert [f.n for f in greenhouse.fields] == list(range(len(greenhouse.fields)))
    assert [b.n for b in greenhouse.buttons] == list(range(len(greenhouse.buttons)))
    ids = [f.id_or_name for f in greenhouse.fields]
    assert ids.index("first_name") < ids.index("email") < ids.index("resume")


def test_resolve_round_trips_a_frame_locator(browser_page, greenhouse):
    first = _by_id(greenhouse, "first_name")
    loc = apply_form.resolve(browser_page, first.locator)
    loc.fill("Jane")
    inner = browser_page.frame_locator("#grnhse_iframe").locator("#first_name")
    assert inner.input_value() == "Jane"
    submit = [b for b in greenhouse.buttons if b.text == "Submit application"][0]
    assert apply_form.resolve(browser_page, submit.locator).count() == 1


def test_digest_to_dict_is_json_serialisable(greenhouse):
    raw = json.loads(json.dumps(greenhouse.to_dict()))
    back = apply_form.FormDigest.from_dict(raw)
    assert back == greenhouse


# --- Lever: label-wrapped inputs ---------------------------------------------------

def test_lever_enclosing_labels_and_name_locators(browser_page, fixture_url):
    browser_page.goto(fixture_url("lever_single.html"))
    d = apply_form.extract(browser_page)
    name = _by_id(d, "name")
    assert name.label == "Full name" and name.required and name.locator == (0, '[name="name"]')
    comments = _by_label(d, "Additional information")
    assert comments.type == "textarea"
    assert comments.placeholder.startswith("Add a cover letter")
    resume = _by_label(d, "Resume/CV")
    assert resume.type == "file" and resume.locator == (0, '[name="resume"]')
    linkedin = _by_label(d, "LinkedIn URL")
    assert linkedin.type == "url" and linkedin.locator == (0, '[name="urls[LinkedIn]"]')
    assert [b.kind_hint for b in d.buttons] == ["submit"]


# --- Ashby: steps, preceding-text labels, button hints ----------------------------

def test_ashby_step_one_only_shows_identity_fields_and_continue(browser_page, fixture_url):
    browser_page.goto(fixture_url("ashby_steps.html"))
    d = apply_form.extract(browser_page)
    assert [f.id_or_name for f in d.fields] == ["first_name", "last_name", "email", "phone"]
    assert [(b.text, b.kind_hint) for b in d.buttons] == [("Continue", "advance")]


def test_ashby_step_two_radio_label_from_preceding_text(browser_page, fixture_url):
    browser_page.goto(fixture_url("ashby_steps.html"))
    browser_page.click("text=Continue")
    d = apply_form.extract(browser_page)
    auth = _by_id(d, "work_auth")
    assert auth.type == "select" and auth.required and auth.options == ["Yes", "No"]
    spons = _by_id(d, "sponsorship")
    assert spons.type == "radio"
    assert spons.label == "Will you now or in the future require sponsorship?"
    assert spons.required is True and spons.options == ["Yes", "No"]
    assert [(b.text, b.kind_hint) for b in d.buttons] == [("Back", "back"), ("Continue", "advance")]


# --- the generic listbox page -------------------------------------------------------

def test_listbox_combobox_labelled_by_aria_labelledby(browser_page, fixture_url):
    browser_page.goto(fixture_url("generic_listbox.html"))
    d = apply_form.extract(browser_page)
    country = _by_label(d, "Country")
    assert country.type == "listbox" and country.required
    assert country.locator == (0, "#country")
    assert country.options == []                 # the menu is built on click
    location = _by_label(d, "Preferred work location")
    assert location.type == "listbox"
    assert location.options == ["Remote", "Hybrid", "On-site"]   # the menu is in the DOM
    # the inner inputs belong to the widget and stay out of the field list
    assert not [f for f in d.fields if f.id_or_name in ("country-input", "location-input")]
    start = _by_label(d, "Available start date")
    assert start.type == "date"


# --- the park pages ------------------------------------------------------------------

def test_captcha_page_text_names_recaptcha(browser_page, fixture_url):
    browser_page.goto(fixture_url("captcha.html"))
    d = apply_form.extract(browser_page)
    assert "reCAPTCHA" in d.text
    assert "Verify you are human" in d.text
    robot = _by_id(d, "not_robot")
    assert robot.type == "checkbox" and robot.label == "I'm not a robot"


def test_login_wall_password_is_other_and_button_is_plain(browser_page, fixture_url):
    browser_page.goto(fixture_url("login_wall.html"))
    d = apply_form.extract(browser_page)
    pw = _by_id(d, "login_password")
    assert pw.type == "other" and pw.label == "Password"
    assert pw.autocomplete == "current-password"
    email = _by_id(d, "login_email")
    assert email.type == "email"
    assert email.autocomplete == "username"
    assert {f["id_or_name"]: f["autocomplete"] for f in d.to_dict()["fields"]} == {
        "login_email": "username", "login_password": "current-password"}
    assert [(b.text, b.kind_hint) for b in d.buttons] == [("Sign in", "")]


def test_essay_textarea_is_required_and_help_carries_the_maxlength(browser_page, fixture_url):
    browser_page.goto(fixture_url("essay_required.html"))
    d = apply_form.extract(browser_page)
    essay = _by_id(d, "project")
    assert essay.type == "textarea" and essay.required
    assert essay.label == "Describe a project you are proud of and your motivation for this role"
    assert essay.help == "Max 1500 characters."
    assert _by_id(d, "first_name").help == ""


def test_preceding_text_walk_stops_at_another_controls_label(browser_page):
    browser_page.set_content("""
      <body>
        <h2>Location</h2>
        <label><input type="checkbox" name="remote_ok"> Remote OK</label>
        <input name="city">
        <div>Preferred office</div>
        <input name="office">
      </body>""")
    d = apply_form.extract(browser_page)
    by = {f.id_or_name: f for f in d.fields}
    assert by["remote_ok"].label == "Remote OK"
    assert by["city"].label == ""            # the neighbour's label text is not the city's
    assert by["office"].label == "Preferred office"


def test_preceding_text_walk_stops_at_a_label_whose_for_target_is_missing(browser_page):
    browser_page.set_content("""
      <body>
        <label for="remote_ok_missing">Remote OK</label>
        <input name="city">
      </body>""")
    d = apply_form.extract(browser_page)
    assert [(f.id_or_name, f.label) for f in d.fields] == [("city", "")]


def test_confirmation_page_has_no_fields(browser_page, fixture_url):
    browser_page.goto(fixture_url("confirmation.html"))
    d = apply_form.extract(browser_page)
    assert d.fields == [] and d.buttons == []
    assert "Your application has been received." in d.text


# --- a LinkedIn posting: site chrome is no form, the Apply link is a button ----------

@pytest.fixture
def linkedin(browser_page, fixture_url):
    browser_page.goto(fixture_url("linkedin_posting.html"))
    return apply_form.extract(browser_page)


def test_linkedin_header_search_and_footer_language_picker_are_not_fields(linkedin):
    # the 2026-09-22 live run read these three as a form and judged the posting
    # `application_form`, then parked with "no way forward on this page"
    assert linkedin.fields == []


def test_linkedin_apply_link_is_a_button_in_document_order(browser_page, linkedin):
    texts = [b.text for b in linkedin.buttons]
    apply = [b for b in linkedin.buttons if b.text == "Apply"]
    assert len(apply) == 1 and apply[0].locator == (0, "#posting-apply")
    assert texts.index("Me") < texts.index("Apply") < texts.index("Saved")
    assert not any("Easy Apply" in t for t in texts)    # a similar job's card
    assert apply_form.resolve(browser_page, apply[0].locator).count() == 1


def test_the_page_text_leads_with_the_main_landmark(linkedin):
    # the judge reads the text's first characters; on the 2026-09-22 run
    # LinkedIn's skip links, header and upsell took 597 of its 600 and the
    # posting read as a form
    assert linkedin.text.startswith("Analytics Engineer")
    assert linkedin.text.count("Own the analytics models") == 1
    assert linkedin.text.rstrip().endswith("Home Me")      # the rest follows, nothing dropped


def test_a_banner_above_main_keeps_its_place_and_the_site_chrome_moves_last(browser_page):
    browser_page.set_content("""
      <body><header><nav>Home Jobs Messaging</nav></header>
        <div>This job is no longer accepting applications</div>
        <main><h1>Analyst</h1><p>About the job</p></main>
        <footer>Privacy</footer></body>""")
    lines = apply_form.extract(browser_page).text.splitlines()
    assert lines[0] == "This job is no longer accepting applications"
    assert lines[1] == "Analyst"
    assert lines[-1] == "Home Jobs Messaging" and "Privacy" in lines


def test_linkedins_real_page_prefix_leaves_the_posting_inside_the_headline(browser_page):
    # the text above "About the job" on the 2026-09-22 page: skip links, the
    # header, the upsell and the top card, 597 characters
    browser_page.set_content("""
      <body><div>0 notifications<br>Skip to main content<br>Skip to primary content<br>
        Skip to aside<br>Skip to footer</div>
      <header><nav>Home<br>My Network<br>Jobs<br>Messaging<br>21<br>Notifications<br>
        <button>Me</button><br><button>For Business</button></nav>
        <p><a href="#">Reactivate Premium: 50% Off</a></p></header>
      <main><p>GTS</p><p>Quantitative Trader - Overnight Session / Asia Trading, ETF Team</p>
        <p>New York, NY - Reposted 2 days ago - Over 100 people clicked apply</p>
        <p>Promoted by hirer - Responses managed off LinkedIn</p>
        <div>Remote<br>Full-time<br><a aria-label="Apply on company website" href="#">Apply</a><br>
          <button>Saved</button><br>Use AI to assess how you fit</div>
        <p>Get AI-powered advice on this job and more exclusive features with Premium.
          Reactivate Premium: 50% Off</p>
        <p><button>Show match details</button></p><p><button>Tailor my resume</button></p>
        <p><button>Help me stand out</button></p>
        <h2>About the job</h2><p>The ETF trading group is seeking a Quantitative Trader.</p>
      </main></body>""")
    body = browser_page.evaluate("document.body.innerText")
    assert "About the job" not in body[:600]           # what the judge saw before
    text = apply_form.extract(browser_page).text
    assert "About the job" in text[:600]         # the header moved last, on its own
    assert "The ETF trading group" in text[:apply_judge.HEADLINE_CHARS]
    assert text.rstrip().endswith("Reactivate Premium: 50% Off")


def test_site_chrome_below_main_never_cuts_text_out_of_a_banner(browser_page):
    browser_page.set_content("""
      <body><div>Privacy notice: this posting closes Friday</div>
        <main><h1>Analyst</h1></main><footer>Privacy</footer></body>""")
    lines = apply_form.extract(browser_page).text.splitlines()
    assert lines[:3] == ["Privacy notice: this posting closes Friday", "Analyst", "Privacy"]


def test_without_a_visible_main_the_text_keeps_page_order(browser_page):
    browser_page.set_content("""
      <body><header>Site menu</header><main hidden>Old step</main>
        <p>Apply for Analyst</p></body>""")
    d = apply_form.extract(browser_page)
    assert d.text.startswith("Site menu") and "Old step" not in d.text


def test_a_form_or_dialog_keeps_the_controls_in_its_own_header_and_footer(browser_page):
    browser_page.set_content("""
      <body>
        <form>
          <header><label for="q">Job title</label><input id="q"></header>
          <footer><label><input type="checkbox" name="agree"> I agree</label>
            <button type="submit">Submit</button></footer>
        </form>
        <div role="dialog"><header><label for="nick">Preferred name</label>
          <input id="nick"></header></div>
        <footer><form><label for="news">Newsletter email</label>
          <input id="news" type="email"></form></footer>
      </body>""")
    d = apply_form.extract(browser_page)
    assert sorted(f.id_or_name for f in d.fields) == ["agree", "nick", "q"]


def test_only_short_apply_links_outside_site_chrome_join_the_buttons(browser_page):
    browser_page.set_content("""
      <body><main>
        <a href="/signup">Create an account</a>
        <a href="/apply">Apply for this job</a>
        <a href="/go" aria-label="Apply on company website"><span>Go</span></a>
        <nav><a href="/jobs/apply">Apply</a></nav>
      </main></body>""")
    d = apply_form.extract(browser_page)
    assert [b.text for b in d.buttons] == ["Apply for this job", "Go"]


# --- a required control is never a posting widget or a cookie banner --------------------

def test_a_required_question_naming_a_newsletter_is_kept(browser_page):
    browser_page.set_content("""
      <body><main><h1>Apply</h1><form>
        <label for=fn>First name *</label><input id=fn name=fn required>
        <label for=src>How did you hear about us (job board, newsletter, referral)? *</label>
        <select id=src name=src><option value="">Select...</option><option>Job board</option>
          <option>Newsletter</option><option>Referral</option></select>
        <label for=alerts>Which job alerts brought you here?</label>
        <input id=alerts name=alerts aria-required=true>
        <label><input type=checkbox name=news> Subscribe to our newsletter</label>
        <button type=submit>Submit application</button></form></main></body>""")
    d = apply_form.extract(browser_page)
    by = {f.id_or_name: f for f in d.fields}
    assert by["src"].required and by["src"].type == "select"
    assert by["alerts"].required
    # an optional newsletter tick box is still the posting's widget
    assert "news" not in by


_GDPR_FORMLESS = """
  <body><main><h1>Apply</h1><div id="app">
    <label for=fn>First name *</label><input id=fn required>
    <div class="gdpr-consent">__BOX__</div>
    <button id=go>Submit application</button></div></main></body>"""


@pytest.mark.parametrize("box, name", [
    # required by its attribute
    ("<label><input type=checkbox id=agree required> I agree to the processing of my data "
     "and the use of cookies as described in the privacy notice *</label>", "agree"),
    # starred only
    ("<label><input type=checkbox id=agree> Data and cookies notice read and accepted *</label>",
     "agree"),
    # an agreement, no mark at all
    ("<label><input type=checkbox id=agree> I consent to the processing of my data and the "
     "use of cookies for this application</label>", "agree"),
])
def test_a_formless_consent_box_holding_the_applications_agreement_is_no_banner(
        browser_page, box, name):
    browser_page.set_content(_GDPR_FORMLESS.replace("__BOX__", box))
    d = apply_form.extract(browser_page)
    assert name in [f.id_or_name for f in d.fields]


def test_the_gate_reads_a_required_agreement_in_a_formless_consent_box(browser_page):
    browser_page.set_content(_GDPR_FORMLESS.replace(
        "__BOX__", "<label><input type=checkbox id=agree required> I agree to the processing of "
        "my data and the use of cookies as described in the privacy notice *</label>"
        "<span role=checkbox id=terms aria-required=true aria-checked=false tabindex=0>"
        "I accept the terms *</span>"))
    browser_page.fill("#fn", "Jane")
    invalid = apply_form.validity_report(browser_page)["invalid"]
    assert "agree" in [r["name"] for r in invalid]
    invalid = apply_form.validity_report(browser_page, (0, "#go"), [(0, "#fn")])["invalid"]
    assert "agree" in [r["name"] for r in invalid]
    scan = apply_form.control_scan(browser_page, required_only=True)
    assert [r["kind"] for r in scan] == ["checkbox"]


def test_a_cookie_banner_with_its_own_tick_boxes_is_still_a_banner(browser_page):
    browser_page.set_content("""
      <body><main><h1>Apply</h1><form>
        <label for=fn>First name</label><input id=fn name=fn>
        <button type=submit>Submit application</button></form></main>
      <div class="cookie-banner" style="position:fixed;bottom:0">We use cookies.
        <label><input type=checkbox name=analytics> Analytics cookies</label>
        <label><input type=checkbox name=marketing checked> Marketing cookies</label>
        <button>Reject all</button><button>Accept all</button></div></body>""")
    d = apply_form.extract(browser_page)
    assert [f.id_or_name for f in d.fields] == ["fn"]
    assert [b.text for b in d.buttons] == ["Submit application"]


def test_the_gate_reads_a_required_box_in_the_footer_beside_a_formless_submit(browser_page):
    browser_page.set_content("""
      <body><main><h1>Apply</h1><div id="app">
        <label for=fn>First name</label><input id=fn required>
        <footer><label><input type=checkbox id=agree required> I accept the terms *</label>
          <button id=go>Submit application</button></footer></div></main>
      <footer><label for=news>Newsletter email</label><input id=news type=email required></footer>
      </body>""")
    browser_page.fill("#fn", "Jane")
    invalid = apply_form.validity_report(browser_page, (0, "#go"), [(0, "#fn")])["invalid"]
    # the footer beside the button is read; the page's own footer is not
    assert [r["name"] for r in invalid] == ["agree"]


def test_a_path_inside_a_shadow_root_names_only_its_own_control(browser_page):
    browser_page.set_content("""<body><div id=host></div><script>
      const r = document.getElementById('host').attachShadow({mode: 'open'});
      r.innerHTML = '<div><div><label>Nickname <input data-k=c></label></div>'
        + '<label>Email <input data-k=d type=email></label></div>';
      </script></body>""")
    d = apply_form.extract(browser_page)
    for f, k in ((_by_label(d, "Nickname"), "c"), (_by_label(d, "Email"), "d")):
        got = apply_form.resolve(browser_page, f.locator).evaluate_all(
            "els => els.map((e) => e.dataset.k)")
        assert got == [k], (f.locator, got)


def test_a_required_box_whose_id_starts_with_hp_is_no_honeypot(browser_page):
    browser_page.set_content("""<body><form>
      <label for=hp-first>First name *</label><input id=hp-first name=hp-first>
      <label for=hp_phone>Phone</label><input id=hp_phone name=hp_phone required>
      <div style="position:absolute;left:-9999px"><input id=hp_trap name=hp_trap></div>
      <label for=hp_note>Nickname</label><input id=hp_note name=hp_note>
      <button>Submit</button></form></body>""")
    d = apply_form.extract(browser_page)
    # a required question is asked of a person; an optional hp- box stays a trap
    assert sorted(f.id_or_name for f in d.fields) == ["hp-first", "hp_phone"]


def test_an_unread_main_frame_is_a_warning_and_an_unread_title_is_empty(caplog):
    class _Frame:
        url = "https://jobs.example.com/apply"

        def evaluate(self, *a, **k):
            raise RuntimeError("Execution context was destroyed")

    class _Page:
        url = "https://jobs.example.com/apply"
        main_frame = _Frame()
        frames = [main_frame]

        def title(self):
            raise RuntimeError("Target page, context or browser has been closed")

    with caplog.at_level("INFO", logger=apply_form.log.name):
        d = apply_form.extract(_Page())
    assert d.title == "" and d.fields == [] and d.buttons == []
    assert any(r.levelname == "WARNING" and "frame 0 skipped" in r.getMessage()
               for r in caplog.records)
