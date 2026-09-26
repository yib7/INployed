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


def test_apply_reads_the_deadline_on_the_injected_clock(browser_page, fixture_url):
    """The deadline is an instant on `clock`, so a runner with its own clock
    (tests, a frozen clock) is honoured; the real clock is the default."""
    browser_page.goto(fixture_url("lever_single.html"))
    d = apply_form.extract(browser_page)
    plan = FillPlan(fields=[_planned(_field(d, "name"), "fill", "Jane Doe")])
    ticks = iter([5.0, 5.0])
    assert apply_fill.apply(browser_page, plan, deadline=10.0, clock=lambda: next(ticks)) != []
    assert apply_fill.apply(browser_page, plan, deadline=10.0, clock=lambda: 10.0) == []


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


# --- click: the tri-state result behind click_button ------------------------------------

def test_click_tells_a_quiet_click_from_one_that_never_landed(browser_page, fixture_url,
                                                              monkeypatch):
    from playwright.sync_api import TimeoutError as PWTimeout
    browser_page.goto(fixture_url("lever_single.html"))
    browser_page.evaluate("document.body.insertAdjacentHTML('beforeend', "
                          "'<button type=\"button\" id=\"noop\">Nothing</button>')")
    d = apply_form.extract(browser_page)
    r = apply_fill.click(browser_page, d, _button(d, "Nothing").n, timeout_s=1)
    assert (r.clicked, r.changed) == (True, False) and bool(r) is False
    r = apply_fill.click(browser_page, d, 42, timeout_s=1)
    assert (r.clicked, r.changed) == (False, False)

    real = apply_form.resolve

    class _Raising:
        @property
        def first(self):
            return self

        def count(self):
            return 1

        def click(self, **kw):
            raise PWTimeout("Timeout 5000ms exceeded")

    monkeypatch.setattr(apply_fill.apply_form, "resolve",
                        lambda page, loc: _Raising() if loc == _button(d, "Nothing").locator
                        else real(page, loc))
    r = apply_fill.click(browser_page, d, _button(d, "Nothing").n, timeout_s=1)
    assert (r.clicked, r.changed) == (False, False)
    assert apply_fill.click_button(browser_page, d, _button(d, "Nothing").n, timeout_s=1) is False


# --- cycle 18 FM-1: a yes or no matches only an option in its own alias set ---------------

@pytest.mark.parametrize("options, value, index", [
    (["Yes - on a work visa (OPT/H-1B)", "U.S. citizen or permanent resident"], "Yes", -1),
    (["Yes, with sponsorship", "Yes, without sponsorship"], "Yes", -1),
    (["No, but I will need sponsorship in the future", "Yes"], "No", -1),
    (["Yes", "No"], "yes", 0),
    (["Y", "N"], "Yes", 0),
])
def test_a_yes_or_no_is_matched_on_the_page_only_by_its_alias_set(options, value, index):
    assert apply_fill._ci_match(value, options) == index


# --- cycle 18 FM-5: a number box takes a plain number, and repairs join no digits ---------

@pytest.mark.parametrize("value, want", [
    ("120k", None), ("3-5", None), ("(555) 123-4567", None), ("Less than 1 year", None),
    ("1.5", "1.5"), ("3", "3"), (" 7 ", "7"), ("", None), ("$120,000", None), ("5+", None)])
def test_number_value_takes_a_plain_number_only(value, want):
    assert apply_fill.number_value(value) == want


def test_a_repair_that_asks_for_digits_joins_no_digits_but_a_phones():
    pf = PlannedField(n=0, locator=(0, "#x"), label="Years", required=True,
                      fact_key="answer_years", value="3-5", option=None, confidence=1.0,
                      action="fill")
    assert apply_fill.repair_value(pf, "Please enter digits only", {}) == "3-5"
    pf.value = "Anytown 12345"
    assert apply_fill.repair_value(pf, "Numbers only", {}) == "Anytown 12345"
    phone = PlannedField(n=1, locator=(0, "#p"), label="Contact", required=True,
                         fact_key="phone", value="(555) 555-0100", option=None,
                         confidence=1.0, action="fill")
    assert apply_fill.repair_value(phone, "Please enter digits only", {}) == "5555550100"
    phone.fact_key = "answer_contact"
    assert apply_fill.repair_value(phone, "Digits only", {"label": "Mobile"}) == "5555550100"


def test_a_number_box_takes_the_phone_only_when_it_names_a_phone(browser_page):
    browser_page.set_content("""<body><form>
      <label>Phone number <input id="p" type="number"></label>
      <label>Years of experience <input id="y" type="number"></label>
      <label>Contact <input id="t" name="tel" type="number"></label></form></body>""")
    d = apply_form.extract(browser_page)
    p, y, t = (next(f for f in d.fields if f.locator[1] == css) for css in ("#p", "#y", "#t"))
    errors: list[dict] = []
    filled = apply_fill.apply(browser_page, FillPlan(fields=[
        _planned(p, "fill", "(555) 555-0100", fact_key="phone"),
        _planned(y, "fill", "(555) 555-0100", fact_key="phone"),
        _planned(t, "fill", "555-555-0100", fact_key="phone")]), errors=errors)
    got = {x.n: x.value for x in filled}
    assert (got[p.n], got[y.n], got[t.n]) == ("5555550100", "", "5555550100")
    assert [e["n"] for e in errors] == [y.n]


# --- cycle 18 FM-4: clear empties a control, and says so --------------------------------

_CLEARABLE = """<body><form>
  <label>Nickname <input id="nick"></label>
  <label>Notes <textarea id="notes"></textarea></label>
  <label>Team <select id="team"><option value="">Select...</option><option>Data</option>
    <option>Platform</option></select></label>
  <label>Size <select id="size"><option>Small</option><option>Large</option></select></label>
  <label><input id="news" type="checkbox"> Send me news</label>
  <fieldset><legend>Shift</legend>
    <label><input type="radio" name="shift" value="day"> Day</label>
    <label><input type="radio" name="shift" value="night"> Night</label></fieldset>
  </form></body>"""


def test_clear_empties_a_box_a_list_and_a_tick_and_says_when_it_cannot(browser_page):
    browser_page.set_content(_CLEARABLE)
    d = apply_form.extract(browser_page)
    by = {f.label.strip(): f for f in d.fields}
    plan = [_planned(by["Nickname"], "fill", "JD"), _planned(by["Notes"], "fill", "Hello"),
            _planned(by["Team"], "select", option="Data"),
            _planned(by["Size"], "select", option="Large"),
            _planned(by["Send me news"], "select", option="checked"),
            _planned(by["Shift"], "select", option="Night")]
    filled = apply_fill.apply(browser_page, FillPlan(fields=plan))
    assert [x.value for x in filled] == ["JD", "Hello", "Data", "Large", "checked", "Night"]
    got = [apply_fill.clear(browser_page, pf) for pf in plan]
    assert got == [True, True, True, False, True, False]
    assert browser_page.locator("#nick").input_value() == ""
    assert browser_page.locator("#notes").input_value() == ""
    assert browser_page.locator("#team").input_value() == ""
    assert not browser_page.locator("#news").is_checked()
    # a control already empty is cleared; one gone is nothing to clear
    assert apply_fill.clear(browser_page, plan[0]) is True
    gone = _planned(by["Nickname"], "fill", "JD")
    gone.locator = (0, "#nothing-here")
    assert apply_fill.clear(browser_page, gone) is True


# --- cycle 18 FM-2: a list whose options were never read takes only a code match --------

_UNREAD_LIST = """<body><form>
  <span id="auth-label">Work authorization</span>
  <div id="auth" role="combobox" aria-labelledby="auth-label" aria-expanded="false"
       aria-haspopup="listbox" aria-controls="auth-menu" tabindex="0">
    <input id="auth-input" type="text" autocomplete="off" aria-labelledby="auth-label"></div>
  <ul id="auth-menu" role="listbox" hidden>
    <li role="option">Yes - on a work visa (OPT/H-1B)</li>
    <li role="option">U.S. citizen or permanent resident</li></ul>
  <script>
    const box = document.getElementById('auth'), menu = document.getElementById('auth-menu');
    const input = document.getElementById('auth-input');
    const open = () => { menu.hidden = false; box.setAttribute('aria-expanded', 'true'); };
    box.addEventListener('click', open);
    input.addEventListener('input', open);
    menu.querySelectorAll('li').forEach((li) => li.onclick = () => {
      input.value = li.textContent; menu.hidden = true;
      box.setAttribute('aria-expanded', 'false'); });
  </script></form></body>"""


def test_a_list_whose_options_were_never_read_takes_no_option_but_its_own_words(browser_page):
    browser_page.set_content(_UNREAD_LIST)
    d = apply_form.extract(browser_page)
    auth = next(f for f in d.fields if f.label.startswith("Work authorization"))
    pf = _planned(auth, "fill", "Yes", fact_key="work_authorized")
    pf.options = []
    errors: list[dict] = []
    filled = apply_fill.apply(browser_page, FillPlan(fields=[pf]), errors=errors)
    assert [e["error"] for e in errors] == ["OptionsUnread"]
    assert filled[0].value == "" and browser_page.locator("#auth-input").input_value() == ""
    # the same list read ahead is the judge's pick: a failed code match is a
    # plain miss there
    pf.options = ["Yes - on a work visa (OPT/H-1B)", "U.S. citizen or permanent resident"]
    errors = []
    apply_fill.apply(browser_page, FillPlan(fields=[pf]), errors=errors)
    assert [e["error"] for e in errors] == ["LookupError"]
    # an exact match still takes its option
    pf.options, pf.value = [], "U.S. citizen or permanent resident"
    filled = apply_fill.apply(browser_page, FillPlan(fields=[pf]), errors=errors)
    assert filled[0].value == "U.S. citizen or permanent resident"


def test_a_list_that_shows_its_options_only_when_typed_in_is_left_blank(browser_page):
    browser_page.set_content(_UNREAD_LIST.replace(
        "box.addEventListener('click', open);", ""))
    d = apply_form.extract(browser_page)
    auth = next(f for f in d.fields if f.label.startswith("Work authorization"))
    pf = _planned(auth, "fill", "Yes", fact_key="work_authorized")
    pf.options = []
    errors: list[dict] = []
    apply_fill.apply(browser_page, FillPlan(fields=[pf]), errors=errors)
    assert [e["error"] for e in errors] == ["OptionsUnread"]
    # the words typed to bring the options are taken out again
    assert browser_page.locator("#auth-input").input_value() == ""
