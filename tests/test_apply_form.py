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


def test_resolve_round_trips_a_frame_locator(browser_page, fixture_url, greenhouse):
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
    # the inner inputs are part of the widget, never fields of their own
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
    email = _by_id(d, "login_email")
    assert email.type == "email"
    assert [(b.text, b.kind_hint) for b in d.buttons] == [("Sign in", "")]


def test_essay_textarea_is_required_and_help_carries_the_maxlength(browser_page, fixture_url):
    browser_page.goto(fixture_url("essay_required.html"))
    d = apply_form.extract(browser_page)
    essay = _by_id(d, "project")
    assert essay.type == "textarea" and essay.required
    assert essay.label == "Describe a project you are proud of"
    assert essay.help == "Max 1500 characters."
    assert _by_id(d, "first_name").help == ""


def test_confirmation_page_has_no_fields(browser_page, fixture_url):
    browser_page.goto(fixture_url("confirmation.html"))
    d = apply_form.extract(browser_page)
    assert d.fields == [] and d.buttons == []
    assert "Your application has been received." in d.text
