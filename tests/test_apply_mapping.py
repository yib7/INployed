"""SP5: what the mapping request carries and how the planner reads a pick.

- READ-10: every button in the mapping request carries its DOM flags
  (`in_form`, `disabled`, `primary`); `FakeJev` never reads a boolean (an
  object's boolean entry goes with its key), so the flags leave its roles as
  they were.

No browser, no network: hand-built digests, the harness's synthetic catalog,
`FakeJev`."""
import copy
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import apply_facts  # noqa: E402
import apply_harness as h  # noqa: E402
import apply_judge  # noqa: E402
import jev  # noqa: E402
from apply_form import Button, Field, FormDigest  # noqa: E402


@pytest.fixture
def catalog(tmp_path):
    folder = h.write_job_folder(tmp_path / "job")
    return apply_facts.build(folder, answers=h.bank())


def _f(n, label, type_="text", required=False, options=(), section=""):
    return Field(n=n, locator=(0, f"#f{n}"), label=label, type=type_, required=required,
                 options=list(options))


def _strip_flags(state):
    out = copy.deepcopy(state)
    for b in out.get("buttons", []):
        for flag in ("in_form", "disabled", "primary"):
            b.pop(flag, None)
    return out


# --- READ-10: the buttons' DOM flags ---------------------------------------------------------

def test_the_mapping_sends_each_buttons_form_disabled_and_primary_flags(catalog):
    digest = FormDigest(url_host="x", title="Apply", text="", fields=[_f(0, "Email", "email")],
                        buttons=[Button(n=0, locator=(0, "#s"), text="Submit application",
                                        in_form=True, disabled=True, primary=True),
                                 Button(n=1, locator=(0, "#h"), text="Help")])
    state, _ = apply_judge.page_questions(digest, catalog, {})
    assert state["buttons"] == [
        {"n": 0, "text": "Submit application", "in_form": True, "disabled": True,
         "primary": True},
        {"n": 1, "text": "Help", "in_form": False, "disabled": False, "primary": False}]
    # the flags round-trip through the digest's JSON
    again = FormDigest.from_dict(digest.to_dict())
    assert (again.buttons[0].disabled, again.buttons[0].primary) == (True, True)


def test_the_fake_judge_reads_no_boolean_so_the_flags_leave_its_roles_as_they_were(catalog):
    texts = ["Submit application", "Apply now", "Continue", "Back", "Next", "Sign in",
             "Create account", "Cancel", "Upload resume", "Save and continue"]
    for flags in ((True, True, True), (True, False, False), (False, True, True)):
        digest = FormDigest(url_host="x", title="Apply", text="Apply for the role",
                            fields=[_f(0, "Email", "email", required=True)],
                            buttons=[Button(n=i, locator=(0, f"#b{i}"), text=t,
                                            in_form=flags[0], disabled=flags[1],
                                            primary=flags[2])
                                     for i, t in enumerate(texts)])
        state, q = apply_judge.page_questions(digest, catalog, {})
        with_flags = jev.FakeJev().judge(state, q)
        without = jev.FakeJev().judge(_strip_flags(state), q)
        assert {k: a.choice for k, a in with_flags.items()} == \
            {k: a.choice for k, a in without.items()}, flags
    # the rule itself: a boolean entry is never text, nor its key
    assert jev._as_text({"text": "Next", "in_form": True, "items": [True, "Back"]}) == \
        '{"text": "Next", "items": ["Back"]}'
    words = jev._flatten({"a": True, "b": "word", "c": [False, "more"]}).split()
    assert words == ["word", "more"]


# --- a question that names code, a review step with its submit ---------------------------------

def test_a_question_of_tick_boxes_that_names_code_is_no_code_box():
    langs = Field(n=0, locator=(0, "#g"), label="Which languages do you write code in?",
                  type="checkbox", required=False, options=["Python", "SQL"])
    promo = Field(n=1, locator=(0, "#s"), label="Code of conduct accepted", type="select",
                  required=True, options=["Yes", "No"])
    digest = FormDigest(url_host="x", title="Apply", text="Apply for the role",
                        fields=[_f(2, "Full name", required=True), langs, promo])
    assert apply_judge.code_field(digest.fields) is None
    assert not apply_judge.page_facts(digest).code_box
    code = _f(3, "Enter the code", required=True)
    assert apply_judge.code_field([langs, code]) is code


def test_a_review_step_with_its_submit_and_no_box_is_the_review_by_its_structure():
    digest = FormDigest(url_host="x", title="Apply", text="Step 2 of 2 Review and submit",
                        buttons=[Button(n=0, locator=(0, "#s"), text="Submit Application")])
    facts = apply_judge.page_facts(digest)
    assert facts.review and facts.send_buttons == 1
    assert apply_judge.structural_kind(facts, strict=True) == "review_page"
    # a page that still asks for something is no review by its structure
    digest.fields = [_f(0, "Email", "email", required=True)]
    assert apply_judge.structural_kind(apply_judge.page_facts(digest), strict=True) != \
        "review_page"


# --- an account screen's own button -----------------------------------------------------------

def test_an_account_screen_never_clicks_the_sites_header_sign_in():
    import apply_run
    from apply_judge import FillPlan
    digest = FormDigest(url_host="x", title="Create Account", text="Create Account", fields=[
        _f(0, "Email Address", required=True)], buttons=[
        Button(n=0, locator=(0, "#h"), text="Sign In", chrome=True),
        Button(n=1, locator=(0, "#s"), text="Search for Jobs", chrome=True),
        Button(n=2, locator=(0, "#c"), text="Create Account")])
    # the header's Sign In judged the advance, the screen's own button other
    plan = FillPlan(buttons={"advance": (0, 0.95), "other": (2, 0.9)})
    assert apply_run.account_advance(digest, plan, signup=True) == (
        2, apply_judge.BUTTON_ADVANCE_MIN_CONF)
    plan = FillPlan(buttons={"advance": (2, 0.9), "other": (0, 0.9)})
    assert apply_run.account_advance(digest, plan, signup=True) == (2, 0.9)
    # two buttons that name the step for the same screen: no guess
    digest.buttons.append(Button(n=3, locator=(0, "#l"), text="Register now"))
    assert apply_run.account_advance(digest, FillPlan(buttons={"advance": (0, 0.95)}),
                                     signup=True) is None


def test_workdays_account_screen_takes_the_button_that_fits_the_screen():
    # review M1: the captured screen draws "Create Account" twice (a click
    # filter over the real button) beside an in-page "Sign In"
    import apply_run
    from apply_judge import FillPlan
    digest = FormDigest(url_host="x", title="Create Account", text="Create Account", fields=[
        _f(0, "Email Address", required=True)], buttons=[
        Button(n=0, locator=(0, "#h"), text="Sign In", chrome=True),
        Button(n=1, locator=(0, "#s"), text="Search for Jobs", chrome=True),
        Button(n=2, locator=(0, "#b"), text="Back to Job Posting"),
        Button(n=3, locator=(0, "#f"), text="Create Account"),
        Button(n=4, locator=(0, "#c"), text="Create Account"),
        Button(n=5, locator=(0, "#i"), text="Sign In")])
    plan = FillPlan(buttons={"advance": (0, 0.97), "back": (2, 0.9)})
    floor = apply_judge.BUTTON_ADVANCE_MIN_CONF
    assert apply_run.account_advance(digest, plan, signup=True) == (3, floor)
    assert apply_run.account_advance(digest, plan, signup=False) == (5, floor)


def test_an_account_step_never_takes_a_judged_button_that_names_the_other_step():
    # the fix round's Workday misses: the judge rates the screen's own "Sign
    # In" (its "Already have an account?" link) the advance above "Create
    # Account", and the sign-up clicked it and landed on the sign-in screen
    import apply_run
    from apply_judge import FillPlan
    digest = FormDigest(url_host="x", title="Create Account", text="Create Account", fields=[
        _f(0, "Email Address", required=True)], buttons=[
        Button(n=0, locator=(0, "#h"), text="Sign In", chrome=True),
        Button(n=1, locator=(0, "#c"), text="Create Account"),
        Button(n=2, locator=(0, "#i"), text="Sign In"),
        Button(n=3, locator=(0, "#b"), text="Back to Job Posting")])
    floor = apply_judge.BUTTON_ADVANCE_MIN_CONF
    plan = FillPlan(buttons={"advance": (2, 0.95), "apply_entry": (3, 0.9)})
    assert apply_run.account_advance(digest, plan, signup=True) == (1, floor)
    # a sign-in screen's judged "Create Account" is no way on for the sign-in
    plan = FillPlan(buttons={"advance": (1, 0.95)})
    assert apply_run.account_advance(digest, plan, signup=False) == (2, floor)
    # a judged button that names neither step is taken as judged
    digest.buttons.append(Button(n=4, locator=(0, "#n"), text="Continue"))
    plan = FillPlan(buttons={"advance": (4, 0.9)})
    assert apply_run.account_advance(digest, plan, signup=True) == (4, 0.9)
    # the judged submit that fits stands in for an advance that does not
    plan = FillPlan(buttons={"advance": (2, 0.95), "submit": (1, 0.9)})
    assert apply_run.account_advance(digest, plan, signup=True) == (1, 0.9)
    # a sign-in screen misread as a sign-up (login_wall_park at seed 17):
    # its only way on says "Sign in", and the step takes it as judged
    login = FormDigest(url_host="x", title="Sign in", text="Sign in", fields=[
        _f(0, "Email", required=True)], buttons=[
        Button(n=0, locator=(0, "#s"), text="Sign in")])
    plan = FillPlan(buttons={"advance": (0, 0.77)})
    assert apply_run.account_advance(login, plan, signup=True) == (0, 0.77)


def test_a_forms_own_next_is_its_way_on_when_the_header_took_the_advance():
    import apply_run
    from apply_judge import FillPlan
    digest = FormDigest(url_host="x", title="My Information", text="Step 2 of 4", fields=[
        _f(0, "First Name", required=True)], buttons=[
        Button(n=0, locator=(0, "#h"), text="Sign In", chrome=True),
        Button(n=1, locator=(0, "#s"), text="Search for Jobs", chrome=True),
        Button(n=2, locator=(0, "#g"), text="Continue with Google"),
        Button(n=3, locator=(0, "#n"), text="Save and Continue")])
    # the header's buttons judged the advance, the page's own Save and Continue other
    plan = FillPlan(buttons={"advance": (1, 0.97), "other": (3, 0.9)})
    assert apply_run.form_route(digest, plan, park_mode=True) == (
        "advance", (3, apply_judge.BUTTON_ADVANCE_MIN_CONF), "")
    # a sign-in's "Continue with ..." is never the way on
    digest.buttons = digest.buttons[:3]
    assert apply_run.form_route(digest, plan, park_mode=True)[0] == "stuck"


@pytest.mark.parametrize("text, taken", [
    ("Save and Continue", True), ("Continue later", False), ("Save and continue later", False),
    ("Continue browsing jobs", False), ("Submit and continue", False),
    ("Continue with LinkedIn", False)])
def test_the_pages_own_next_leaves_a_later_a_browse_and_a_send(text, taken):
    # review M2: only a Next that goes on with the application is taken
    import apply_run
    from apply_judge import FillPlan
    digest = FormDigest(url_host="x", title="My Information", text="Step 2 of 4", fields=[
        _f(0, "First Name", required=True)], buttons=[
        Button(n=0, locator=(0, "#h"), text="Sign In", chrome=True),
        Button(n=1, locator=(0, "#n"), text=text)])
    plan = FillPlan(buttons={"advance": (0, 0.97), "other": (1, 0.9)})
    step, button, _ = apply_run.form_route(digest, plan, park_mode=True)
    assert (step == "advance" and button[0] == 1) is taken, (step, button)
    # a disabled Next waits
    digest.buttons[1] = Button(n=1, locator=(0, "#n"), text="Save and Continue", disabled=True)
    assert apply_run.form_route(digest, plan, park_mode=True)[0] == "stuck"
