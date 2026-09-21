"""`apply_fill` over the local HTML fixtures (headless Chromium): filling by
plan, read-back values, listbox opening, button clicks that wait for the DOM
or a navigation. No network; skips without Playwright or Chromium."""
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("playwright")

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import apply_fill  # noqa: E402
import apply_form  # noqa: E402
import apply_judge  # noqa: E402
from apply_judge import FillPlan, PlannedField  # noqa: E402

pytest_plugins = ["conftest_browser"]

_PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[]/Count 0>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"


def _field(digest, id_or_name):
    hits = [f for f in digest.fields if f.id_or_name == id_or_name]
    assert hits, f"no field {id_or_name!r}; have {[f.id_or_name for f in digest.fields]}"
    return hits[0]


def _button(digest, text):
    hits = [b for b in digest.buttons if b.text == text]
    assert hits, f"no button {text!r}; have {[b.text for b in digest.buttons]}"
    return hits[0]


def _planned(f, action, value="", option=None, fact_key=None):
    return PlannedField(n=f.n, locator=f.locator, label=f.label, required=f.required,
                        fact_key=fact_key, value=value, option=option, confidence=0.9,
                        action=action)


@pytest.fixture
def pdf(tmp_path):
    p = tmp_path / "Jane_Doe_Resume.pdf"
    p.write_bytes(_PDF)
    return p


# --- (c) apply: text, select by label, radio, checkbox, upload; read-back values ----

def test_apply_fills_the_greenhouse_iframe_and_reads_back(browser_page, fixture_url, pdf):
    browser_page.goto(fixture_url("greenhouse_embed.html"))
    browser_page.frame_locator("#grnhse_iframe").locator("#first_name").wait_for()
    d = apply_form.extract(browser_page)
    plan = FillPlan(fields=[
        _planned(_field(d, "first_name"), "fill", "Jane", fact_key="first_name"),
        _planned(_field(d, "email"), "fill", "jane@example.com", fact_key="email"),
        _planned(_field(d, "hear_about"), "select", option="LinkedIn"),
        _planned(_field(d, "work_auth"), "select", option="yes"),        # case-insensitive
        _planned(_field(d, "certify"), "select", option="checked"),
        _planned(_field(d, "resume"), "upload", str(pdf), fact_key="resume_file"),
        _planned(_field(d, "phone"), "skip"),
        _planned(_field(d, "linkedin"), "generate"),
    ])
    filled = apply_fill.apply(browser_page, plan)
    by_n = {x.n: x for x in filled}
    assert by_n[_field(d, "first_name").n].value == "Jane"
    assert by_n[_field(d, "email").n].value == "jane@example.com"
    assert by_n[_field(d, "hear_about").n].value == "LinkedIn"
    assert by_n[_field(d, "work_auth").n].value == "Yes"
    assert by_n[_field(d, "certify").n].value == "checked"
    assert by_n[_field(d, "resume").n].value == "Jane_Doe_Resume.pdf"
    assert _field(d, "phone").n not in by_n and _field(d, "linkedin").n not in by_n
    assert [x.label for x in filled][:2] == ["First Name", "Email"]
    assert filled[0].to_dict() == {"n": filled[0].n, "label": "First Name", "value": "Jane"}
    # the DOM agrees with the read-back
    inner = browser_page.frame_locator("#grnhse_iframe")
    assert inner.locator("#hear_about").input_value() == "1"
    assert inner.locator("input[name=work_auth][value=yes]").is_checked()
    assert inner.locator("#certify").is_checked()
    assert not browser_page.evaluate("!!document.body.getAttribute('data-submitted')")


def test_apply_textarea_and_lever_name_locators(browser_page, fixture_url, pdf):
    browser_page.goto(fixture_url("lever_single.html"))
    d = apply_form.extract(browser_page)
    plan = FillPlan(fields=[
        _planned(_field(d, "name"), "fill", "Jane Doe"),
        _planned(_field(d, "comments"), "fill", "Happy to share more on request."),
        _planned(_field(d, "resume"), "upload", str(pdf)),
    ])
    filled = apply_fill.apply(browser_page, plan)
    assert [x.value for x in filled] == ["Jane Doe", "Happy to share more on request.",
                                         "Jane_Doe_Resume.pdf"]


def test_apply_date_accepts_iso_and_us_formats(browser_page, fixture_url):
    browser_page.goto(fixture_url("generic_listbox.html"))
    d = apply_form.extract(browser_page)
    start = _field(d, "start_date")
    filled = apply_fill.apply(browser_page, FillPlan(fields=[_planned(start, "fill", "2026-10-01")]))
    assert filled[0].value == "2026-10-01"
    filled = apply_fill.apply(browser_page, FillPlan(fields=[_planned(start, "fill", "11/15/2026")]))
    assert filled[0].value == "2026-11-15"


def test_apply_failed_action_logs_and_continues(browser_page, fixture_url):
    browser_page.goto(fixture_url("lever_single.html"))
    d = apply_form.extract(browser_page)
    bogus = PlannedField(n=99, locator=(0, "#does-not-exist"), label="Ghost", required=False,
                         fact_key=None, value="x", option=None, confidence=0.9, action="fill")
    lines = []
    plan = FillPlan(fields=[bogus, _planned(_field(d, "name"), "fill", "Jane Doe")])
    filled = apply_fill.apply(browser_page, plan, log=lines.append)
    assert [(x.n, x.value) for x in filled] == [(99, ""), (_field(d, "name").n, "Jane Doe")]
    assert any("Ghost" in line for line in lines)


# --- (b) the React-select style listbox --------------------------------------------

def test_open_listbox_options_reports_the_menu_then_closes_it(browser_page, fixture_url):
    browser_page.goto(fixture_url("generic_listbox.html"))
    d = apply_form.extract(browser_page)
    country = _field(d, "country")
    assert country.options == []
    assert apply_fill.open_listbox_options(browser_page, country) == ["United States", "Canada", "Other"]
    assert browser_page.get_attribute("#country", "aria-expanded") == "false"


def test_open_listbox_options_without_aria_controls_finds_the_visible_menu(browser_page, fixture_url):
    browser_page.goto(fixture_url("generic_listbox.html"))
    browser_page.evaluate("document.getElementById('country').removeAttribute('aria-controls')")
    d = apply_form.extract(browser_page)
    country = _field(d, "country")
    assert apply_fill.open_listbox_options(browser_page, country) == ["United States", "Canada", "Other"]
    filled = apply_fill.apply(browser_page, FillPlan(fields=[_planned(country, "select", option="Other")]))
    assert filled[0].value == "Other"


def test_apply_select_on_a_listbox_clicks_the_matching_option(browser_page, fixture_url):
    browser_page.goto(fixture_url("generic_listbox.html"))
    d = apply_form.extract(browser_page)
    plan = FillPlan(fields=[_planned(_field(d, "country"), "select", option="Canada"),
                            _planned(_field(d, "location"), "select", option="remote")])
    filled = apply_fill.apply(browser_page, plan)
    assert [x.value for x in filled] == ["Canada", "Remote"]
    assert browser_page.get_attribute("#country", "data-value") == "Canada"


# --- (d) click_button: DOM change, navigation, marker, timeout --------------------

def test_click_button_waits_for_the_ashby_step_change(browser_page, fixture_url):
    browser_page.goto(fixture_url("ashby_steps.html"))
    d = apply_form.extract(browser_page)
    assert apply_fill.click_button(browser_page, d, _button(d, "Continue").n) is True
    d2 = apply_form.extract(browser_page)
    ids = [f.id_or_name for f in d2.fields]
    assert ids == ["work_auth", "sponsorship"]
    assert browser_page.url.endswith("ashby_steps.html")       # no navigation happened


def test_click_button_submit_sets_the_marker_on_lever(browser_page, fixture_url):
    browser_page.goto(fixture_url("lever_single.html"))
    d = apply_form.extract(browser_page)
    assert apply_fill.click_button(browser_page, d, _button(d, "Submit application").n) is True
    assert browser_page.get_attribute("body", "data-submitted") == "1"
    assert "Thank you for applying" in apply_fill.page_text(browser_page)


def test_click_button_follows_a_navigation(browser_page, fixture_url):
    browser_page.goto(fixture_url("login_wall.html"))
    browser_page.fill("#login_email", "jane@example.com")
    browser_page.fill("#login_password", "not-a-real-password")
    d = apply_form.extract(browser_page)
    assert apply_fill.click_button(browser_page, d, _button(d, "Sign in").n) is True
    assert browser_page.url.endswith("ashby_steps.html")
    assert "first_name" in [f.id_or_name for f in apply_form.extract(browser_page).fields]


def test_click_button_settles_before_returning_on_a_delayed_navigation(browser_page, fixture_url):
    browser_page.goto(fixture_url("login_wall.html"))
    browser_page.fill("#login_password", "not-a-real-password")
    # the navigation starts 300 ms after the click, so the poll sees the page leave mid-flight
    browser_page.evaluate("""() => {
        const b = document.getElementById('btn-signin');
        b.replaceWith(b.cloneNode(true));
        document.getElementById('btn-signin').addEventListener('click', () => {
            setTimeout(() => { window.location.href = 'ashby_steps.html'; }, 300);
        });
    }""")
    d = apply_form.extract(browser_page)
    assert apply_fill.click_button(browser_page, d, _button(d, "Sign in").n) is True
    assert browser_page.url.endswith("ashby_steps.html")
    assert "first_name" in [f.id_or_name for f in apply_form.extract(browser_page).fields]


def test_click_button_outlasts_a_spinner_that_precedes_the_navigation(browser_page, fixture_url):
    browser_page.goto(fixture_url("login_wall.html"))
    browser_page.fill("#login_password", "not-a-real-password")
    # the click shows a spinner at once (a DOM change) and the navigation lands 800 ms later
    browser_page.evaluate("""() => {
        const b = document.getElementById('btn-signin');
        b.replaceWith(b.cloneNode(true));
        document.getElementById('btn-signin').addEventListener('click', () => {
            document.body.insertAdjacentHTML('beforeend', '<div id="spinner">Signing in...</div>');
            setTimeout(() => { window.location.href = 'ashby_steps.html'; }, 800);
        });
    }""")
    d = apply_form.extract(browser_page)
    assert apply_fill.click_button(browser_page, d, _button(d, "Sign in").n) is True
    assert browser_page.url.endswith("ashby_steps.html")
    ids = [f.id_or_name for f in apply_form.extract(browser_page).fields]
    assert ids == ["first_name", "last_name", "email", "phone"]


def test_click_button_routes_the_torn_down_frame_path_through_settle(browser_page, fixture_url, monkeypatch):
    browser_page.goto(fixture_url("lever_single.html"))
    d = apply_form.extract(browser_page)
    real_snapshot = apply_fill._snapshot
    calls = {"snapshots": 0, "settled": 0}

    def torn_down(page):
        calls["snapshots"] += 1
        return (("gone",),) if calls["snapshots"] > 1 else real_snapshot(page)

    monkeypatch.setattr(apply_fill, "_snapshot", torn_down)
    monkeypatch.setattr(apply_fill, "_settle",
                        lambda page, timeout_s, **kw: calls.__setitem__("settled", calls["settled"] + 1))
    assert apply_fill.click_button(browser_page, d, _button(d, "Submit application").n) is True
    assert calls["settled"] == 1


def test_apply_stops_at_a_passed_deadline(browser_page, fixture_url):
    browser_page.goto(fixture_url("lever_single.html"))
    d = apply_form.extract(browser_page)
    lines = []
    plan = FillPlan(fields=[_planned(_field(d, "name"), "fill", "Jane Doe")])
    filled = apply_fill.apply(browser_page, plan, log=lines.append, deadline=time.monotonic() - 1)
    assert filled == []
    assert browser_page.input_value('[name="name"]') == ""
    assert any("deadline" in line for line in lines)


def test_apply_with_a_future_deadline_fills(browser_page, fixture_url):
    browser_page.goto(fixture_url("lever_single.html"))
    d = apply_form.extract(browser_page)
    plan = FillPlan(fields=[_planned(_field(d, "name"), "fill", "Jane Doe")])
    filled = apply_fill.apply(browser_page, plan, deadline=time.monotonic() + 30)
    assert [x.value for x in filled] == ["Jane Doe"]


def test_click_button_returns_false_when_nothing_changes(browser_page, fixture_url):
    browser_page.goto(fixture_url("lever_single.html"))
    browser_page.evaluate("document.body.insertAdjacentHTML('beforeend', "
                          "'<button type=\"button\" id=\"noop\">Nothing</button>')")
    d = apply_form.extract(browser_page)
    assert apply_fill.click_button(browser_page, d, _button(d, "Nothing").n, timeout_s=1) is False


def test_click_button_unknown_n_is_false(browser_page, fixture_url):
    browser_page.goto(fixture_url("lever_single.html"))
    d = apply_form.extract(browser_page)
    assert apply_fill.click_button(browser_page, d, 42, timeout_s=1) is False


def test_page_text_is_capped(browser_page, fixture_url):
    browser_page.goto(fixture_url("greenhouse_embed.html"))
    browser_page.frame_locator("#grnhse_iframe").locator("#first_name").wait_for()
    text = apply_fill.page_text(browser_page)
    assert "Apply for this job" in text and "Voluntary Self-Identification" in text
    assert len(text) <= apply_judge.PAGE_TEXT_CAP
    browser_page.evaluate("document.body.insertAdjacentText('beforeend', 'x'.repeat(10000))")
    assert len(apply_fill.page_text(browser_page)) == apply_judge.PAGE_TEXT_CAP


# --- wait_for_change: the click_button wait without the click ---------------------------

def test_wait_for_change_sees_a_late_dom_change_and_a_quiet_page(browser_page, fixture_url):
    browser_page.goto(fixture_url("slow_submit.html"))
    t0 = time.monotonic()
    assert apply_fill.wait_for_change(browser_page, timeout_s=1.0) is False
    assert time.monotonic() - t0 < 3
    browser_page.click("#btn-submit")                      # the change lands 3 s later
    assert apply_fill.wait_for_change(browser_page, timeout_s=10.0) is True
    assert browser_page.locator("#thanks").is_visible()
    assert browser_page.evaluate("window.__clicks") == 1
